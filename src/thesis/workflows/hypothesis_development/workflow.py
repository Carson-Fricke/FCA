"""The falsification-first hypothesis-development orchestrator.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import NoReturn

from thesis.agent.context import PromptContext, assemble_context
from thesis.agent.router import InvocationResult, ModelRouter, Role
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.contracts import (
    MessageRole,
    RecordNotFound,
    TranscriptItemKind,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionState,
    replace_record,
)
from thesis.chat.service import ChatService
from thesis.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventType,
)
from thesis.execution.sub_agents import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubAgentContextSpec,
    append_sub_agent_event,
)
from thesis.experiments.ledger import AttemptKind, AttemptLedger, AttemptRecord
from thesis.persistence.journal import ArtifactRef, CommandJournal, content_hash
from thesis.research.hypothesis import (
    HypothesisDocument,
    HypothesisFrontMatter,
    HypothesisLinks,
    HypothesisStatus,
    HypothesisStore,
    InMemoryHypothesisStore,
)
from thesis.research.models import ResearchModel
from thesis.strategies.bundles import StrategyBundle, StrategyBundleError
from thesis.strategies.validation import (
    StrategySandboxValidator,
    StrategyValidationError,
    validate_strategy_bundle,
)
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
    HarnessError,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunner,
    ResearchModelRunRequest,
    ResearchModelRunResult,
)

from .composers import (
    JsonCreationTurnComposer,
    JsonDevelopmentHypothesisComposer,
    JsonFalsificationPlanner,
    JsonRcaComposer,
    JsonStrategyDesigner,
    _json_document,
)
from .contracts import (
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
    FalsifierTest,
    HypothesisDevelopmentRequest,
    HypothesisDraft,
)
from .errors import (
    CreationTurn,
    FalsificationComposeError,
    HypothesisDevelopmentIntakeError,
    InvalidPhaseTransition,
    WorkflowError,
    _Fenced,
    _PhaseFailure,
)
from .gates import (
    check_evidence_bound,
    check_falsifiability,
    check_falsification_plan,
    check_research_model,
    check_strategy_design,
    check_verdict_advances,
)
from .phases import (
    _ACTIVE_PHASES,
    _COMPLETED,
    _FAILED,
    _FALSIFICATION_TRIALS,
    _HYPOTHESIS_CREATION,
    _OPTIMIZATION,
    _PAUSED,
    _PHASE_ORCHESTRATORS,
    _PHASE_TASKS,
    _REQUESTED,
    _STRATEGY_BACKTESTING,
    _STRATEGY_DESIGN,
    _STRATEGY_RCA,
    _TERMINAL_EXECUTION_STATES,
    _VERDICT,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentPhase,
    PhaseTransition,
    _ActiveSubAgent,
    transition_phase,
)
from .primitives import (
    _CREATION_TURN_SCHEMA,
    _FALSIFICATION_PLAN_SCHEMA,
    _HYPOTHESIS_MEDIA_TYPE,
    _HYPOTHESIS_SCHEMA,
    _JSON_ONLY_INSTRUCTION,
    _MODEL_TOOL_NAME,
    _RCA_SCHEMA,
    _STRATEGY_DESIGN_SCHEMA,
    _TOOL_NAME,
    _WORKFLOW_ACTOR,
    _WORKFLOW_ID,
    _WORKFLOW_VERSION,
    DEFAULT_MAX_COMPOSE_REPAIRS,
    DEFAULT_MAX_HYPOTHESIS_CYCLES,
    DEFAULT_MAX_RCA_CYCLES,
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    _ComposedT,
    _digest,
    _format_observed,
    _iso,
    _metric,
    _new_id,
    _text,
    _utc_now,
)
from .prompts import (
    FalsificationPlanner,
    HypothesisComposer,
    RcaComposer,
    StrategyDesigner,
    _creation_finalize_prompt,
    _creation_turn_prompt,
    _event_output_text,
    _falsification_turn_prompt,
    _hypothesis_context_lines,
)
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

#: Structured intent the verdict gate asks the model to return.
_VERDICT_INTENT_SCHEMA = (
    '{"action": "check_off"|"circle_back"|"ambiguous", "message": string}'
)


class HypothesisDevelopmentWorkflow:
    """Orchestrate the falsification-first chat-to-strategy pipeline."""

    def __init__(
        self,
        *,
        chat: ChatService,
        router: ModelRouter,
        runner: BacktestRunner,
        research_runner: ResearchModelRunner | None = None,
        validator: StrategySandboxValidator,
        hypothesis_store: HypothesisStore | None = None,
        composer: HypothesisComposer | None = None,
        planner: FalsificationPlanner | None = None,
        rca_composer: RcaComposer | None = None,
        designer: StrategyDesigner | None = None,
        ledger: AttemptLedger | None = None,
        subagent_factory: SubagentContextFactory | None = None,
        journal: CommandJournal | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        research_role: Role = "research",
        critique_role: Role = "critique",
        coding_role: Role = "coding",
        experiment_id: str | None = None,
        max_rca_cycles: int = DEFAULT_MAX_RCA_CYCLES,
        max_compose_repairs: int = DEFAULT_MAX_COMPOSE_REPAIRS,
        knowledge_cutoff: datetime | None = None,
        subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._chat = chat
        self._router = router
        self._runner = runner
        self._research_runner = (
            BacktestResearchModelRunner(runner)
            if research_runner is None
            else research_runner
        )
        if not callable(getattr(self._research_runner, "run", None)):
            raise WorkflowError("research_runner must implement ResearchModelRunner")
        self._validator = validator
        self._store = (
            InMemoryHypothesisStore() if hypothesis_store is None else hypothesis_store
        )
        self._composer = (
            JsonDevelopmentHypothesisComposer() if composer is None else composer
        )
        self._planner = JsonFalsificationPlanner() if planner is None else planner
        self._rca_composer = JsonRcaComposer() if rca_composer is None else rca_composer
        self._designer = JsonStrategyDesigner() if designer is None else designer
        self._ledger = ledger
        self._subagents = (
            SubagentContextFactory(clock=clock, id_factory=lambda: id_factory("ctx"))
            if subagent_factory is None
            else subagent_factory
        )
        self._journal = journal
        self._clock = clock
        self._id_factory = id_factory
        self._research_role = research_role
        self._critique_role = critique_role
        self._coding_role = coding_role
        self._experiment_id = experiment_id
        self._max_hypothesis_cycles = DEFAULT_MAX_HYPOTHESIS_CYCLES
        if not isinstance(max_rca_cycles, int) or max_rca_cycles < 0:
            raise WorkflowError("max_rca_cycles must be a non-negative integer")
        self._max_rca_cycles = max_rca_cycles
        if not isinstance(max_compose_repairs, int) or max_compose_repairs < 0:
            raise WorkflowError("max_compose_repairs must be a non-negative integer")
        self._max_compose_repairs = max_compose_repairs
        if knowledge_cutoff is not None and (
            knowledge_cutoff.tzinfo is None or knowledge_cutoff.utcoffset() is None
        ):
            raise WorkflowError("knowledge_cutoff must be timezone-aware")
        self._knowledge_cutoff = knowledge_cutoff
        self._subagent_mcp_tools: dict[str, tuple[str, ...]] = {
            family: tuple(tools)
            for family, tools in dict(subagent_mcp_tools or {}).items()
        }
        self._active_orchestrators: dict[str, _ActiveSubAgent] = {}
        self._paused_states: dict[str, HypothesisDevelopmentState] = {}

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        return HYPOTHESIS_DEVELOPMENT_WORKFLOW

    @property
    def store(self) -> HypothesisStore:
        """Return the hypothesis-artifact store this workflow projects onto."""
        return self._store

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request.

        When ``trigger_message_id`` is supplied the execution binds to the
        operator's existing message instead of creating a duplicate trigger.
        """
        try:
            _text(ideas, "ideas", max_length=8_192)
        except WorkflowError as error:
            raise HypothesisDevelopmentIntakeError(str(error)) from error
        if trigger_message_id is None:
            execution = self._chat.launch_workflow(
                workspace_id,
                chat_id,
                workflow=self.definition,
                actor_id=actor_id,
                body=ideas,
                investigation_id=investigation_id,
            )
        else:
            execution = self._chat.launch_workflow(
                workspace_id,
                chat_id,
                workflow=self.definition,
                actor_id=actor_id,
                message_id=trigger_message_id,
                investigation_id=investigation_id,
            )
        self._chat.create_message(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            body=self.definition.title,
            role=MessageRole.SYSTEM,
            kind=TranscriptItemKind.WORKFLOW,
            data={
                "currentPhase": execution.phase,
                "executionId": execution.execution_id,
                "status": execution.state.value,
                "title": self.definition.title,
                "phases": list(self.definition.phases),
                "version": execution.workflow_version,
                "workflowId": execution.workflow_id,
            },
            execution_id=execution.execution_id,
        )
        return HypothesisDevelopmentRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            ideas=ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
        )

    def steer(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        body: str,
        message_id: str | None = None,
    ) -> WorkflowExecution:
        """Record immediate, non-destructive steering on the execution."""
        return self._chat.steer_workflow(
            workspace_id,
            execution_id,
            actor_id=actor_id,
            body=body,
            message_id=message_id,
        )

    def observe(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay:
        """Return an ordered replay page for reconnecting clients."""
        return self._chat.replay_events(
            workspace_id,
            execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def run(
        self, request: HypothesisDevelopmentRequest
    ) -> HypothesisDevelopmentOutcome:
        """Open the hypothesis-creation conversation for this request."""
        state = HypothesisDevelopmentState(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=_REQUESTED,
            intent_epoch=request.intent_epoch,
        )
        try:
            self._require_execution(request)
            self._check_stale_request(request)
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            state = self._transition(
                request, state, _HYPOTHESIS_CREATION, "chat request accepted"
            )
            state = self._hypothesis_creation(request, state)
            state = self._run_after_creation(request, state)
        except _Fenced as fence:
            if self._execution_cancelled_or_deleted(request):
                return self._outcome(
                    replace(
                        fence.state if fence.state is not None else state,
                        detail=str(fence),
                        fenced=True,
                    )
                )
            state = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except FalsificationComposeError as error:
            state = self._pause_for_compose(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except (
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except Exception as error:  # noqa: BLE001 - unexpected infra faults fail closed
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        return self._outcome(state)

    def resume(
        self, request: HypothesisDevelopmentRequest
    ) -> HypothesisDevelopmentOutcome:
        """Run one more hypothesis-creation turn after operator input.

        Called when the operator sends a message to the hypothesis-creation
        node.  ``run`` was a single synchronous pass; this continuation re-runs
        one turn against the durable conversation and, once the operator
        supplies enough information, lets the model judge whether to advance,
        and then runs the remaining phases.
        """
        self._chat.get_execution(request.workspace_id, request.execution_id)
        paused_state = self._paused_states.get(request.execution_id)
        if paused_state is not None and paused_state.phase is _VERDICT:
            return self._resume_verdict(request, paused_state)
        state = HypothesisDevelopmentState(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=_HYPOTHESIS_CREATION,
            intent_epoch=request.intent_epoch,
            transitions=self._replay_transitions(request),
            hypothesis_cycles=sum(
                1
                for transition in self._replay_transitions(request)
                if transition.to_phase is _FALSIFICATION_TRIALS
            ),
        )
        try:
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            state = self._hypothesis_creation(request, state)
            state = self._run_after_creation(request, state)
        except _Fenced as fence:
            if self._execution_cancelled_or_deleted(request):
                return self._outcome(
                    replace(
                        fence.state if fence.state is not None else state,
                        detail=str(fence),
                        fenced=True,
                    )
                )
            state = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except FalsificationComposeError as error:
            state = self._pause_for_compose(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except (
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except Exception as error:  # noqa: BLE001 - unexpected infra faults fail closed
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        return self._outcome(state)

    def _resume_verdict(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentOutcome:
        """Apply operator input to the durable verdict conversation."""
        try:
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            active = self._activate_orchestrator(request, _VERDICT)
            body = self._latest_sub_agent_user_message(request, active.sub_agent_id)
            action = self._interpret_verdict_intent(request, state, active, body)
            if action is None:
                return self._request_operator_input(
                    request,
                    state,
                    purpose="apply the falsification verdict",
                    detail=(
                        "the operator's reply did not clearly check off or circle "
                        "back the recorded verdict"
                    ),
                    active=active,
                )
            self._paused_states.pop(request.execution_id, None)
            return self._outcome(
                self._continue_after_verdict(request, state, action=action)
            )
        except _Fenced as fence:
            if self._execution_cancelled_or_deleted(request):
                return self._outcome(
                    replace(
                        fence.state if fence.state is not None else state,
                        detail=str(fence),
                        fenced=True,
                    )
                )
            paused = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, paused.phase, {"state": paused.phase.value})
            return self._outcome(paused)
        except _PhaseFailure as failure:
            failed = self._fail(request, failure.state, failure)
            self._emit_phase(request, failed.phase, {"state": failed.phase.value})
            return self._outcome(failed)
        except FalsificationComposeError as error:
            paused = self._pause_for_compose(request, state, error)
            self._emit_phase(request, paused.phase, {"state": paused.phase.value})
            return self._outcome(paused)
        except (
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            failed = self._fail(request, state, error)
            self._emit_phase(request, failed.phase, {"state": failed.phase.value})
            return self._outcome(failed)
        except Exception as error:  # noqa: BLE001 - unexpected infra faults fail closed
            failed = self._fail(request, state, error)
            self._emit_phase(request, failed.phase, {"state": failed.phase.value})
            return self._outcome(failed)

    def _interpret_verdict_intent(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
        body: str,
    ) -> str | None:
        """Interpret the operator's verdict reply with the model, not keywords.

        The phase agent reads the operator's prose and returns a structured
        intent. Ambiguity or an unavailable model returns ``None`` so the
        workflow asks the operator again instead of guessing.
        """
        verdict = state.verdict
        if verdict is None:
            return None
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-verdict-intent",
        )
        task = "\n".join(
            [
                "You are the workflow's verdict gate.",
                f"Recorded falsification verdict: {verdict.outcome}. {verdict.reason}",
                f"Operator's latest message: {body}",
                "",
                "Decide the operator's intent: 'check_off' when the reply clearly "
                "accepts or applies the recorded verdict, 'circle_back' when it "
                "clearly asks to revise the hypothesis, or 'ambiguous' when it is "
                "unclear. Reply with exactly one JSON object and nothing else:",
                _VERDICT_INTENT_SCHEMA,
            ]
        )
        try:
            invocation = self._router.invoke(
                self._research_role,
                prompt_for(task),
                response_format={"type": "json_object"},
            )
            document = _json_document(invocation, "verdict intent")
        except Exception:  # noqa: BLE001 - an unavailable model is not a decision
            return None
        action = document.get("action")
        if isinstance(action, str) and action in {"check_off", "circle_back"}:
            return action
        return None

    def _run_after_creation(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Run the bounded premise and strategy loops to a terminal outcome."""
        document = state.hypothesis
        if document is None:
            raise WorkflowError("falsification trials require a hypothesis")
        state = self._run_trials(request, state)
        # The agent evaluates the trials first, in prose, including any harness
        # or data failure, before the evidence gate decides whether the verdict
        # can be trusted. This is the agent-first replacement for a separate
        # falsification RCA step and guarantees failures reach the model.
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            prose = self._author_falsification_prose(request, state, active)
            state = replace(state, falsification_prose=prose or None)
            if prose:
                self._emit_orchestrator_text(request, prose)
        state = self._transition_checked(
            request,
            state,
            _VERDICT,
            "evidence bound to every planned falsifier",
            lambda: check_evidence_bound(state.plan, state.trials),
        )
        verdict = self._evaluate_verdict(state)
        state = self._project_hypothesis(request, state, status=verdict.outcome)
        state = replace(state, verdict=verdict)
        self._append_attempt(
            request,
            state,
            AttemptKind.METRIC,
            "falsification verdict recorded",
            verdict.as_dict(),
        )
        revised_question = self._emit_verdict_review(request, state)
        raise _Fenced(revised_question, state)

    def _continue_after_verdict(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        action: str,
    ) -> HypothesisDevelopmentState:
        """Apply a checked-off verdict or an explicit circle-back request."""
        verdict = state.verdict
        if verdict is None:
            raise WorkflowError("verdict review requires a recorded verdict")
        document = state.hypothesis
        if document is None:
            raise WorkflowError("verdict review requires a hypothesis")
        if action == "circle_back" or check_verdict_advances(verdict) is not None:
            state = self._transition(
                request,
                state,
                _HYPOTHESIS_CREATION,
                "premise refuted; returning to hypothesis creation",
            )
            self._post_phase_handoff(
                request,
                self._premise_handoff(state, verdict),
                kind="falsification_findings",
            )
            if state.hypothesis_cycles >= self._max_hypothesis_cycles:
                return self._stop_for_cycle_budget(
                    request,
                    state,
                    purpose="recover from an exhausted hypothesis revision budget",
                    detail=(
                        "the hypothesis revision budget of 5 attempts is exhausted "
                        "after repeated falsifications; review the falsification "
                        "history and provide operator direction"
                    ),
                )
            state = replace(
                state,
                hypothesis=None,
                draft=None,
                research_model_reference=None,
                research_model_artifact=None,
                fitted_model_reference=None,
                fitted_parameters=None,
                research_model_runtime_configuration=None,
                plan=None,
                trials=(),
                strategy_bundle=None,
                exit_criteria=(),
                strategy_backtest=None,
                optimization_backtest=None,
            )
            return self._hypothesis_creation(request, state)
        state = self._transition(
            request,
            state,
            _STRATEGY_DESIGN,
            "hypothesis survived falsification",
        )
        state = self._design(request, state)
        state = self._transition_checked(
            request,
            state,
            _STRATEGY_BACKTESTING,
            "strategy validated with falsifier exit criteria",
            lambda: check_strategy_design(
                state.strategy_bundle, state.exit_criteria, document.front_matter
            ),
        )
        state = self._run_strategy_backtest(request, state)
        while True:
            state = self._revise_until_strategy_test_passes(request, state, document)
            state = self._transition(
                request,
                state,
                _OPTIMIZATION,
                "strategy backtest completed successfully",
            )
            state = self._optimize(request, state)
            if (
                state.optimization_backtest is None
                or state.optimization_backtest.succeeded
            ):
                break
            state = replace(state, strategy_backtest=state.optimization_backtest)
        journal_id = self._journal_entry(request, state)
        state = replace(state, journal_id=journal_id)
        self._finish_execution(
            request,
            WorkflowExecutionState.COMPLETED,
            ExecutionEventType.WORKFLOW_COMPLETED,
            payload=self._completion_payload(request, state),
        )
        return self._transition(request, state, _COMPLETED, "outcome recorded")

    def _emit_verdict_review(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> str:
        """Render the verdict as a conversational review, not a workflow gate."""
        verdict = state.verdict
        active = self._active_orchestrators.get(request.execution_id)
        if verdict is None or active is None:
            raise WorkflowError("verdict review requires an active verdict node")
        question = self._operator_question(
            request,
            state,
            active=active,
            purpose="review and apply the falsification verdict",
            detail=(
                f"verdict {verdict.outcome}: {verdict.reason}. The operator may "
                "check off the verdict to apply it, or circle back to revise the "
                "hypothesis."
            ),
        )
        lines = [
            "### Falsification Verdict",
            "",
            f"**{verdict.outcome.capitalize()}**: {verdict.reason}",
            "",
            question,
        ]
        self._emit_orchestrator_text(request, "\n".join(lines))
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=_VERDICT,
            role=active.role,
            context=active.context,
            accepts_input=True,
            extra={
                "kind": "verdict_review",
                "question": question,
                "verdict": verdict.outcome,
            },
        )
        return question

    def _latest_sub_agent_user_message(
        self, request: HypothesisDevelopmentRequest, sub_agent_id: str
    ) -> str:
        """Return the latest operator message addressed to one phase node."""
        for message in reversed(
            self._chat.list_messages(request.workspace_id, request.chat_id)
        ):
            if message.role is not MessageRole.USER:
                continue
            data = message.data
            if isinstance(data, Mapping) and data.get("subAgentId") == sub_agent_id:
                return message.body
        return ""

    # -- hypothesis creation conversation ----------------------------------

    def _hypothesis_creation(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Run one structured model judgment, finalizing when it advances."""
        active = self._ensure_creation_orchestrator(request)
        conversation = self._creation_conversation(request, active.sub_agent_id)
        first_turn = not any(role == "assistant" for role, _ in conversation)
        revision_turn = bool(conversation and conversation[-1][0] == "system")
        reply = self._creation_reply(
            request, active, conversation, first_turn=first_turn
        )
        text = reply.message.strip() or (
            "Tell me more about the regularity you expect so I can refine the model."
        )
        if not first_turn and not revision_turn and reply.advance:
            self._emit_orchestrator_text(request, text)
            return self._finalize_creation(request, state, active)
        if revision_turn:
            purpose = (
                "confirm the revised hypothesis proposal before running "
                "falsification trials"
            )
        elif reply.advance:
            purpose = "review the proposed hypothesis model before finalizing"
        else:
            purpose = "refine the hypothesis model"
        question = self._operator_question(
            request,
            state,
            active=active,
            purpose=purpose,
            detail=(
                f"hypothesis attempt {state.hypothesis_cycles + 1}; the proposal is "
                "ready for operator review"
                if reply.advance
                else f"hypothesis attempt {state.hypothesis_cycles + 1}; the model needs "
                "more operator context to refine the proposal"
            ),
        )
        self._emit_orchestrator_text(request, f"{text}\n\n{question}")
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=True,
            extra={
                "question": question,
                "ready": reply.advance and not revision_turn,
            },
        )
        raise _Fenced(question, state)

    def _finalize_creation(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
    ) -> HypothesisDevelopmentState:
        """Compose the structured hypothesis + plan and hand off to trials."""
        conversation = self._creation_conversation(request, active.sub_agent_id)
        draft, invocation = self._compose_creation_hypothesis(request, conversation)
        document = self._build_hypothesis(request, draft)
        failure = check_falsifiability(document.front_matter)
        if failure is not None:
            return self._ask_creation_question(request, state, active, failure)
        model_failure = check_research_model(draft.model, document.front_matter)
        if model_failure is not None:
            return self._ask_creation_question(request, state, active, model_failure)
        self._store.save(document)
        state = replace(
            state,
            draft=draft,
            hypothesis=document,
            hypothesis_cycles=state.hypothesis_cycles + 1,
            model_provenance=MappingProxyType(dict(invocation.provenance.as_dict())),
        )
        self._append_attempt(
            request,
            state,
            AttemptKind.HYPOTHESIS,
            "falsifiable hypothesis authored",
            {
                "hypothesisId": document.hypothesis_id,
                "disagreement": document.front_matter.disagreement,
                "falsifiers": [
                    falsifier.model_dump(mode="json")
                    for falsifier in document.front_matter.falsifiers
                ],
            },
        )
        plan = self._plan(request, state)
        plan_failure = check_falsification_plan(plan, document.front_matter)
        if plan_failure is not None:
            return self._ask_creation_question(request, state, active, plan_failure)
        state = replace(state, plan=plan)
        self._append_attempt(
            request,
            state,
            AttemptKind.PARAMETER,
            "falsification plan recorded",
            {
                "tests": [test.test_id for test in plan.tests],
                "controls": [control.control_id for control in plan.controls],
            },
        )
        self._emit_orchestrator_text(
            request,
            f"Hypothesis finalization recorded. {len(plan.tests)} falsification "
            "trial(s) planned.",
        )
        self._finish_orchestrator(request, status=STATUS_COMPLETED)
        return self._transition(
            request, state, _FALSIFICATION_TRIALS, "falsifiable hypothesis confirmed"
        )

    def _ask_creation_question(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
        question: str,
    ) -> HypothesisDevelopmentState:
        """Surface a blocking clarification without losing the conversation."""
        return self._request_operator_input(
            request,
            state,
            purpose="finalize the hypothesis model",
            detail=question,
            active=active,
        )

    def _creation_reply(
        self,
        request: HypothesisDevelopmentRequest,
        active: _ActiveSubAgent,
        conversation: list[tuple[str, str]],
        *,
        first_turn: bool,
    ) -> CreationTurn:
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=active.sub_agent_id,
        )
        task = _creation_turn_prompt(request.ideas, conversation, first_turn=first_turn)
        turn, _ = self._compose_with_repair(
            request,
            self._research_role,
            base_task=task,
            prompt_for=prompt_for,
            compose=JsonCreationTurnComposer().compose,
            response_schema=_CREATION_TURN_SCHEMA,
            emit_activity=False,
        )
        return turn

    def _compose_creation_hypothesis(
        self,
        request: HypothesisDevelopmentRequest,
        conversation: list[tuple[str, str]],
    ) -> tuple[HypothesisDraft, InvocationResult]:
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=f"Thesis {self._research_role} hypothesis author",
            context_id=f"{request.execution_id}-finalize",
        )
        return self._compose_with_repair(
            request,
            self._research_role,
            base_task=_creation_finalize_prompt(request.ideas, conversation),
            prompt_for=prompt_for,
            compose=self._composer.compose,
            response_schema=_HYPOTHESIS_SCHEMA,
            emit_output=False,
        )

    def _ensure_creation_orchestrator(
        self, request: HypothesisDevelopmentRequest
    ) -> _ActiveSubAgent:
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            return active
        for event in reversed(
            self._chat.replay_events(request.workspace_id, request.execution_id).events
        ):
            if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
                continue
            payload = event.payload
            if (
                payload.get("phase") != _HYPOTHESIS_CREATION.value
                or payload.get("parentSubAgentId") is not None
            ):
                continue
            active = _ActiveSubAgent(
                sub_agent_id=str(payload["subAgentId"]),
                phase=_HYPOTHESIS_CREATION,
                role=str(
                    payload.get("role") or "Thesis hypothesis-creation orchestrator"
                ),
                context=SubAgentContextSpec(),
            )
            self._active_orchestrators[request.execution_id] = active
            return active
        self._spawn_orchestrator(request, _HYPOTHESIS_CREATION)
        return self._active_orchestrators[request.execution_id]

    def _creation_conversation(
        self, request: HypothesisDevelopmentRequest, sub_agent_id: str
    ) -> list[tuple[str, str]]:
        """Reconstruct the durable hypothesis-creation transcript in order."""
        turns: list[tuple[float, str, str]] = [(float("-inf"), "user", request.ideas)]
        for message in self._chat.list_messages(request.workspace_id, request.chat_id):
            if message.kind is not TranscriptItemKind.SUB_AGENT:
                continue
            data = message.data
            if (
                not isinstance(data, Mapping)
                or str(data.get("subAgentId")) != sub_agent_id
                or message.role not in {MessageRole.USER, MessageRole.SYSTEM}
            ):
                continue
            turns.append(
                (
                    message.created_at.timestamp(),
                    "user" if message.role is MessageRole.USER else "system",
                    message.body,
                )
            )
        for event in self._chat.replay_events(
            request.workspace_id, request.execution_id
        ).events:
            if event.payload.get("subAgentId") != sub_agent_id:
                continue
            if event.event_type is ExecutionEventType.SUB_AGENT_OUTPUT:
                text = _event_output_text(event.payload)
                if text:
                    turns.append((event.occurred_at.timestamp(), "assistant", text))
        turns.sort(key=lambda turn: turn[0])
        return [(role, text) for _, role, text in turns]

    def _emit_orchestrator_text(
        self, request: HypothesisDevelopmentRequest, text: str
    ) -> None:
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or not text.strip():
            return
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=active.phase in {_HYPOTHESIS_CREATION, _VERDICT},
            extra={
                "artifact": {"content": text.strip(), "type": "text"},
                "kind": "artifact",
            },
        )

    # -- phases ------------------------------------------------------------

    def _prompt_builder(
        self, *, workspace_scope: str, role: str, context_id: str
    ) -> Callable[[str], PromptContext]:
        """Return a function that renders one task into a scoped prompt."""

        def build(task: str) -> PromptContext:
            return assemble_context(
                workspace_scope=workspace_scope,
                task=task,
                role=role,
                context_id=context_id,
            )

        return build

    def _compose_with_repair(
        self,
        request: HypothesisDevelopmentRequest,
        role: Role,
        *,
        base_task: str,
        prompt_for: Callable[[str], PromptContext],
        compose: Callable[[InvocationResult], _ComposedT],
        response_schema: str,
        emit_activity: bool = True,
        emit_output: bool = True,
    ) -> tuple[_ComposedT, InvocationResult]:
        """Invoke the model and compose, repairing malformed output in place.

        A compose failure never ends the run by itself: the model is re-invoked
        with the validation error and the required schema, up to
        ``max_compose_repairs`` times.  When repairs are exhausted the final
        :class:`FalsificationComposeError` is re-raised so ``run`` can pause for
        operator input instead of failing.

        When ``emit_activity`` is set, the phase orchestrator's task is recorded
        as a ``sub_agent.progress`` event. ``emit_output`` controls whether the
        raw model reply is also recorded; structured calls disable it and emit a
        concise domain summary after composition instead.
        """
        if emit_activity:
            self._emit_orchestrator_progress(request, base_task)
        last_error: FalsificationComposeError | None = None
        invocation: InvocationResult | None = None
        for _ in range(self._max_compose_repairs + 1):
            task = f"{base_task}\n\n{_JSON_ONLY_INSTRUCTION}"
            if last_error is not None:
                task = (
                    f"{base_task}\n\nYour previous reply could not be used: "
                    f"{last_error}. Reply again with a single JSON object that "
                    f"matches this schema: {response_schema}\n\n"
                    f"{_JSON_ONLY_INSTRUCTION}"
                )
            invocation = self._router.invoke(
                role,
                prompt_for(task),
                response_format={"type": "json_object"},
            )
            try:
                composed = compose(invocation)
            except FalsificationComposeError as error:
                last_error = error
                continue
            if emit_activity and emit_output:
                self._emit_orchestrator_output(request, invocation)
            return composed, invocation
        assert invocation is not None and last_error is not None
        raise last_error

    def _build_hypothesis(
        self, request: HypothesisDevelopmentRequest, draft: HypothesisDraft
    ) -> HypothesisDocument:
        hypothesis_id = draft.hypothesis_id or f"h-{request.execution_id}"
        front_matter = HypothesisFrontMatter(
            hypothesis_id=hypothesis_id,
            status=HypothesisStatus.ACTIVE,
            claim=draft.claim,
            disagreement=draft.disagreement,
            risk_factor=draft.risk_factor,
            horizon=draft.horizon,
            universe=draft.universe,
            falsifiers=draft.falsifiers,
            sizing=draft.sizing,
            links=HypothesisLinks(),
        )
        return HypothesisDocument(front_matter=front_matter, body=draft.body)

    def _plan(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> FalsificationPlan:
        document = state.hypothesis
        if document is None or state.draft is None:
            raise WorkflowError("falsification planning requires a hypothesis")
        model_reference = state.draft.model.reference(request.workspace_id).key
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=f"Thesis {self._research_role} falsification planner",
            context_id=f"{request.execution_id}-plan",
        )
        draft, _ = self._compose_with_repair(
            request,
            self._research_role,
            base_task="\n".join(
                [
                    "Design falsification trials and negative controls for the "
                    "hypothesis below. Every test binds to one declared falsifier "
                    "by its falsifierId and describes how that observable is "
                    "computed from historical data. Every control is a "
                    "negative/placebo check (shuffled or permuted returns, an "
                    "unrelated asset pair, a placebo period) that should show no "
                    "regularity. The plan exists to destroy the hypothesis, not to "
                    "confirm it.",
                    "",
                    *_hypothesis_context_lines(document, model_reference),
                ]
            ),
            prompt_for=prompt_for,
            compose=self._planner.compose,
            response_schema=_FALSIFICATION_PLAN_SCHEMA,
            emit_output=False,
        )
        falsifiers = {
            falsifier.falsifier_id: falsifier
            for falsifier in document.front_matter.falsifiers
            if falsifier.falsifier_id is not None
        }
        tests: list[FalsifierTest] = []
        for planned in draft.tests:
            falsifier = falsifiers.get(planned.falsifier_id)
            if falsifier is None:
                raise FalsificationComposeError(
                    f"plan test {planned.test_id!r} references an undeclared "
                    f"falsifier {planned.falsifier_id!r}"
                )
            tests.append(
                FalsifierTest(
                    test_id=planned.test_id,
                    falsifier_id=planned.falsifier_id,
                    observable=falsifier.observable,
                    condition=falsifier.condition,
                    threshold=falsifier.threshold,
                    description=planned.description,
                )
            )
        return FalsificationPlan(tests=tuple(tests), controls=draft.controls)

    def _run_trials(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        plan = state.plan
        if document is None or plan is None or state.draft is None:
            raise WorkflowError("trials require a hypothesis and a falsification plan")
        falsifiers = {
            falsifier.falsifier_id: falsifier
            for falsifier in document.front_matter.falsifiers
            if falsifier.falsifier_id is not None
        }
        retained = {
            trial.test_id: trial for trial in state.trials if trial.observed is not None
        }
        trials: list[FalsificationTrial] = []
        model_reference = state.research_model_reference
        model_artifact = state.research_model_artifact
        fitted_parameters = state.fitted_parameters
        runtime_configuration = state.research_model_runtime_configuration
        fitted_reference = state.fitted_model_reference
        model_run: ResearchModelRunResult | None = None
        model_error: str | None = None
        if any(test.test_id not in retained for test in plan.tests):
            try:
                model_run = self._run_research_model(
                    request,
                    state,
                    model=state.draft.model,
                    correlation_id=f"falsify-{request.execution_id}-model",
                    label="layer-a-model",
                )
            except (BacktestRunError, HarnessError, ValueError) as error:
                model_error = str(error)
            else:
                model_reference = model_run.model_reference
                model_artifact = model_run.workspace_artifact
                fitted_parameters = model_run.fitted_parameters
                runtime_configuration = model_run.runtime_configuration
                fitted_reference = model_run.fitted_reference
        for test in plan.tests:
            existing = retained.get(test.test_id)
            if existing is not None:
                trials.append(existing)
                continue
            if model_error is not None:
                trials.append(
                    FalsificationTrial(
                        test_id=test.test_id,
                        falsifier_id=test.falsifier_id,
                        run_id=f"falsify-{request.execution_id}-model",
                        digest=_digest({"error": model_error}),
                        run_state="failed",
                        observed=None,
                        falsified=False,
                        detail=model_error,
                    )
                )
                self._append_attempt(
                    request,
                    state,
                    AttemptKind.ABORTED_JOB,
                    f"falsification trial {test.test_id} could not run",
                    {
                        "error": model_error,
                        "falsifierId": test.falsifier_id,
                    },
                )
                continue
            assert model_run is not None
            result = model_run.backtest
            sample_valid = _metric(result.metrics, "sample_valid")
            if sample_valid is not None and sample_valid <= 0:
                observed = None
            else:
                observed = _metric(result.metrics, test.observable)
            falsifier = falsifiers[test.falsifier_id]
            detail = result.error
            if detail is None and not result.succeeded:
                detail = f"harness run ended in state {result.state!r}"
            trials.append(
                FalsificationTrial(
                    test_id=test.test_id,
                    falsifier_id=test.falsifier_id,
                    run_id=result.job_id,
                    digest=_digest(result.as_dict()),
                    run_state=result.state,
                    observed=observed,
                    falsified=observed is not None and falsifier.falsifies(observed),
                    detail=detail,
                )
            )
            self._append_attempt(
                request,
                state,
                AttemptKind.METRIC,
                f"falsification trial {test.test_id} recorded",
                {
                    "detail": detail,
                    "falsifierId": test.falsifier_id,
                    "observed": observed,
                    "runId": result.job_id,
                    "runState": result.state,
                },
            )
        state = replace(
            state,
            trials=tuple(trials),
            research_model_reference=model_reference,
            research_model_artifact=model_artifact,
            fitted_model_reference=fitted_reference,
            fitted_parameters=fitted_parameters,
            research_model_runtime_configuration=runtime_configuration,
        )
        return self._project_hypothesis(request, state)

    def _evaluate_verdict(
        self, state: HypothesisDevelopmentState
    ) -> FalsificationVerdict:
        document = state.hypothesis
        if document is None:
            raise WorkflowError("verdict requires a hypothesis")
        by_falsifier = {trial.falsifier_id: trial for trial in state.trials}
        missing: list[str] = []
        for falsifier in document.front_matter.falsifiers:
            key = falsifier.falsifier_id
            trial = None if key is None else by_falsifier.get(key)
            if trial is None or trial.observed is None:
                missing.append(key or falsifier.observable)
        if missing:
            return FalsificationVerdict(
                outcome=VERDICT_INCONCLUSIVE,
                reason=(
                    "insufficient evidence: no numeric observation is bound for "
                    f"{sorted(missing)}"
                ),
                trials=state.trials,
            )
        hits = [trial for trial in state.trials if trial.falsified]
        if hits:
            return FalsificationVerdict(
                outcome=VERDICT_REFUTED,
                reason=(
                    "at least one falsifier was observed: "
                    + ", ".join(sorted(trial.falsifier_id for trial in hits))
                ),
                trials=state.trials,
            )
        return FalsificationVerdict(
            outcome=VERDICT_SUPPORTED,
            reason=(
                "no falsifier was observed across the planned trials; the "
                "hypothesis survived falsification"
            ),
            trials=state.trials,
        )

    def _run_strategy_backtest(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Run one strategy attempt and retain a failed result for RCA."""
        bundle = state.strategy_bundle
        if bundle is None:
            raise WorkflowError("strategy backtesting requires a strategy bundle")
        run_id = f"strategy-test-{request.execution_id}-{state.strategy_cycles}"
        try:
            result = self._backtest(
                request,
                state,
                bundle=bundle,
                correlation_id=run_id,
                label="strategy-backtesting",
            )
        except (BacktestRunError, HarnessError) as error:
            result = BacktestRunResult(
                job_id=run_id,
                state="failed",
                metadata={"error": str(error)},
            )
        self._append_attempt(
            request,
            state,
            AttemptKind.VARIANT,
            "strategy backtest recorded",
            result.as_dict(),
        )
        return replace(state, strategy_backtest=result)

    def _revise_until_strategy_test_passes(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        document: HypothesisDocument,
    ) -> HypothesisDevelopmentState:
        """Run bounded strategy RCA/design cycles until one test succeeds."""
        while (
            state.strategy_backtest is not None
            and not state.strategy_backtest.succeeded
        ):
            state = self._transition(
                request,
                state,
                _STRATEGY_RCA,
                "strategy backtest failed; diagnosing the implementation",
            )
            state = self._diagnose_strategy(request, state)
            if state.strategy_cycles >= self._max_rca_cycles:
                return self._stop_for_cycle_budget(
                    request,
                    state,
                    purpose="recover from an exhausted strategy revision budget",
                    detail=(
                        "the strategy revision budget of 5 attempts is exhausted "
                        "after repeated failed backtests; review the backtest "
                        "lineage and provide operator direction"
                    ),
                )
            state = self._transition(
                request,
                state,
                _STRATEGY_DESIGN,
                "strategy RCA recommendations ready for revision",
            )
            self._post_phase_handoff(
                request,
                self._strategy_handoff(state),
                kind="strategy_rca_findings",
            )
            state = self._design(request, state)
            state = self._transition_checked(
                request,
                state,
                _STRATEGY_BACKTESTING,
                "revised strategy validated with falsifier exit criteria",
                lambda current=state: check_strategy_design(
                    current.strategy_bundle,
                    current.exit_criteria,
                    document.front_matter,
                ),
            )
            state = self._run_strategy_backtest(request, state)
        return state

    def _diagnose_strategy(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Ask the persistent strategy RCA node to diagnose one failed run."""
        result = state.strategy_backtest or state.optimization_backtest
        if result is None:
            raise WorkflowError("strategy RCA requires a failed backtest")
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or active.phase is not _STRATEGY_RCA:
            raise WorkflowError("strategy RCA orchestrator is not active")
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-strategy-rca-{state.strategy_cycles}",
        )
        task = "\n".join(
            [
                "Diagnose this failed strategy backtest. Separate harness/data defects "
                "from implementation, strategy, or market-regime defects. Return a "
                "concrete revision handoff for the strategy-design agent.",
                "Set falsifierId to strategy_backtest and choose a concise failureClass.",
                f"Strategy attempt: {state.strategy_cycles}",
                f"Backtest result: {json.dumps(result.as_dict(), default=str, sort_keys=True)}",
                f"Backtest metadata: {json.dumps(dict(result.metadata), default=str, sort_keys=True)}",
                f"Strategy manifest: {json.dumps(state.strategy_bundle.manifest() if state.strategy_bundle else {}, default=str, sort_keys=True)}",
                f"Required schema: {_RCA_SCHEMA}",
            ]
        )
        diagnosis, _ = self._compose_with_repair(
            request,
            self._critique_role,
            base_task=task,
            prompt_for=prompt_for,
            compose=self._rca_composer.compose,
            response_schema=_RCA_SCHEMA,
            emit_output=False,
        )
        diagnosis = replace(diagnosis, falsifier_id="strategy_backtest")
        prose = "\n".join(
            [
                "### Strategy Backtest RCA",
                "",
                diagnosis.rationale,
                "",
                *[f"- {follow_up}" for follow_up in diagnosis.follow_ups],
            ]
        )
        self._emit_orchestrator_text(request, prose)
        self._append_attempt(
            request,
            state,
            AttemptKind.ABORTED_JOB,
            "strategy RCA diagnosis recorded",
            diagnosis.as_dict(),
        )
        return replace(
            state,
            rca=(*state.rca, diagnosis),
        )

    def _post_phase_handoff(
        self,
        request: HypothesisDevelopmentRequest,
        body: str,
        *,
        kind: str,
    ) -> None:
        """Persist a system handoff into the currently active phase thread."""
        active = self._active_orchestrators.get(request.execution_id)
        if active is None:
            raise WorkflowError("phase handoff requires an active orchestrator")
        self._chat.create_message(
            request.workspace_id,
            request.chat_id,
            actor_id=_WORKFLOW_ACTOR,
            body=body,
            role=MessageRole.SYSTEM,
            execution_id=request.execution_id,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "executionId": request.execution_id,
                "handoffKind": kind,
                "phase": active.phase.value,
                "subAgentId": active.sub_agent_id,
            },
        )

    def _premise_handoff(
        self,
        state: HypothesisDevelopmentState,
        verdict: FalsificationVerdict,
    ) -> str:
        """Render falsification findings for the next hypothesis proposal turn."""
        lines = [
            f"Falsification findings from hypothesis attempt {state.hypothesis_cycles}:",
            verdict.reason,
        ]
        tests = {test.test_id: test for test in state.plan.tests} if state.plan else {}
        for trial in state.trials:
            planned = tests.get(trial.test_id)
            lines.append(
                f"- {trial.test_id}: {planned.observable if planned else trial.falsifier_id} "
                f"observed={_format_observed(trial.observed)}; "
                f"falsified={str(trial.falsified).lower()}"
            )
        lines.append(
            "Revise the statistical premise, model, horizon, or controls in response "
            "to these findings, then present the updated proposal for operator review."
        )
        return "\n".join(lines)

    def _strategy_handoff(self, state: HypothesisDevelopmentState) -> str:
        """Render the latest strategy RCA finding for the next design attempt."""
        diagnosis = state.rca[-1]
        lines = [
            f"Strategy RCA handoff after attempt {state.strategy_cycles}:",
            f"Failure class: {diagnosis.failure_class}",
            diagnosis.rationale,
            "Required revisions:",
            *[f"- {item}" for item in diagnosis.follow_ups],
        ]
        return "\n".join(lines)

    def _stop_for_cycle_budget(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        purpose: str,
        detail: str,
    ) -> HypothesisDevelopmentState:
        """Stop a loop that exhausted its auditable attempt budget."""
        message = self._operator_question(
            request,
            state,
            active=self._active_orchestrators.get(request.execution_id),
            purpose=purpose,
            detail=detail,
        )
        raise _PhaseFailure(message, state)

    def _design(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        model_reference = state.research_model_reference
        fitted_reference = state.fitted_model_reference
        fitted_parameters = state.fitted_parameters
        runtime_configuration = state.research_model_runtime_configuration
        if (
            document is None
            or model_reference is None
            or fitted_reference is None
            or state.draft is None
            or fitted_parameters is None
            or runtime_configuration is None
        ):
            raise WorkflowError("strategy design requires a hypothesis")
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=f"Thesis {self._coding_role} strategy author",
            context_id=f"{request.execution_id}-design",
        )
        revision_context: list[str] = []
        if state.rca:
            latest = state.rca[-1]
            revision_context = [
                "",
                "Strategy RCA handoff from the previous failed backtest:",
                f"Failure class: {latest.failure_class}",
                f"Diagnosis: {latest.rationale}",
                "Required revisions:",
                *[f"- {item}" for item in latest.follow_ups],
            ]
        draft, _ = self._compose_with_repair(
            request,
            self._coding_role,
            base_task="\n".join(
                [
                    "Implement a strategy for the surviving hypothesis below. The "
                    "workspace research model is a referenced dependency for the "
                    "entry logic and Layer B monitor; do not copy or encode the "
                    "indicator/model as a strategy. Load and use the frozen Layer A "
                    "fit; never call fit again or estimate replacement parameters. "
                    "Enter when its observations say "
                    "the regularity is present, "
                    "and declare exit criteria that re-check the declared "
                    "falsifiers (by falsifierId) on a trailing window, so that a "
                    "live breakdown of the historical regularity closes the "
                    "position. Publish each current observation under the exact "
                    "falsifier observable with context.publish_metric(name, value) "
                    "so the run record and exit decision share evidence. Make "
                    "the source define the declared Python strategy entrypoint. "
                    "Sizing and risk limits must be explicit. PnL is a guardrail, never an "
                    "exit criterion.",
                    "",
                    *_hypothesis_context_lines(
                        document, model_reference.key, fitted_reference.key
                    ),
                    *revision_context,
                ]
            ),
            prompt_for=prompt_for,
            compose=self._designer.compose,
            response_schema=_STRATEGY_DESIGN_SCHEMA,
            emit_output=False,
        )
        monitor = dict(runtime_configuration)
        strategy_parameters: Mapping[str, object] = {}
        configured_instances = draft.content.harness_configuration.get(
            "strategy_instances"
        )
        if (
            isinstance(configured_instances, Sequence)
            and not isinstance(configured_instances, str)
            and configured_instances
        ):
            first_instance = configured_instances[0]
            if isinstance(first_instance, Mapping) and isinstance(
                first_instance.get("parameters"), Mapping
            ):
                strategy_parameters = dict(first_instance["parameters"])
        draft = replace(
            draft,
            content=replace(
                draft.content,
                harness_configuration={
                    **dict(draft.content.harness_configuration),
                    "strategy_instances": [
                        {
                            "implementation": "harness.strategy.adapter:StrategyBundleAdapter",
                            "instance_id": draft.content.strategy_id,
                            "bundle_hash": "0" * 64,
                            "capital_fraction": "1.0",
                            "symbols": list(document.front_matter.universe),
                            "parameters": {
                                "strategy_source": dict(draft.content.source),
                                "strategy_entrypoint": draft.content.entrypoint,
                                "strategy_parameters": dict(strategy_parameters),
                                "model_monitor": monitor,
                                "exit_criteria": [
                                    {
                                        **criterion.as_dict(),
                                        "threshold": str(criterion.threshold),
                                    }
                                    for criterion in draft.exit_criteria
                                ],
                            },
                        }
                    ],
                },
                dependencies=tuple(
                    dict.fromkeys(
                        (
                            *draft.content.dependencies,
                            model_reference.key,
                            fitted_reference.key,
                        )
                    )
                ),
            ),
        )
        bundle = draft.content.bundle()
        validate_strategy_bundle(bundle, self._validator)
        self._append_attempt(
            request,
            state,
            AttemptKind.CANDIDATE,
            "sandbox-validated strategy designed",
            {
                "strategyId": bundle.strategy_id,
                "version": bundle.version,
                "bundleSha256": bundle.bundle_sha256,
                "exitCriteria": [
                    criterion.as_dict() for criterion in draft.exit_criteria
                ],
            },
        )
        return replace(
            state,
            strategy_bundle=bundle,
            exit_criteria=draft.exit_criteria,
            strategy_cycles=state.strategy_cycles + 1,
            strategy_backtest=None,
            optimization_backtest=None,
        )

    def _optimize(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        bundle = state.strategy_bundle
        if bundle is None:
            raise WorkflowError("optimization requires a validated strategy bundle")
        result = self._backtest(
            request,
            state,
            bundle=bundle,
            correlation_id=f"optimize-{request.execution_id}",
            label="optimization",
        )
        state = replace(state, optimization_backtest=result)
        self._append_attempt(
            request,
            state,
            AttemptKind.VARIANT,
            "strategy optimization run recorded",
            {"runId": result.job_id, "state": result.state},
        )
        return state

    def _run_research_model(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        model: ResearchModel,
        correlation_id: str,
        label: str,
    ) -> ResearchModelRunResult:
        """Publish and run one workspace model behind a visible tool job."""
        experiment_id = self._experiment_id or f"experiment-{request.execution_id}"
        job = self._chat.submit_tool_job(
            request.workspace_id,
            request.execution_id,
            tool_name=_MODEL_TOOL_NAME,
            actor_id=_WORKFLOW_ACTOR,
            correlation_id=correlation_id,
        )
        self._chat.start_tool_job(
            request.workspace_id, job.tool_job_id, actor_id=_WORKFLOW_ACTOR
        )
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_PROGRESS,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "kind": "tool",
                    "toolCall": {
                        "callId": job.tool_job_id,
                        "tool": _MODEL_TOOL_NAME,
                    },
                },
            )
        try:
            model_run = self._research_runner.run(
                ResearchModelRunRequest(
                    run_id=job.correlation_id,
                    workspace_id=request.workspace_id,
                    model=model,
                    workflow_execution_id=request.execution_id,
                    experiment_id=experiment_id,
                    universe=(
                        state.hypothesis.front_matter.universe
                        if state.hypothesis is not None
                        else ()
                    ),
                )
            )
        except Exception as error:
            self._chat.record_tool_result(
                request.workspace_id,
                job.tool_job_id,
                result=None,
                actor_id=_WORKFLOW_ACTOR,
                success=False,
                error_message=str(error),
                intent_epoch=state.intent_epoch,
            )
            if active is not None:
                self._append_sub_agent(
                    request,
                    ExecutionEventType.SUB_AGENT_PROGRESS,
                    sub_agent_id=active.sub_agent_id,
                    parent_sub_agent_id=None,
                    phase=active.phase,
                    role=active.role,
                    context=active.context,
                    accepts_input=False,
                    extra={
                        "kind": "tool",
                        "toolResult": {
                            "callId": job.tool_job_id,
                            "failure": {"message": str(error)},
                            "ok": False,
                            "structured": {},
                            "summary": "Falsification execution failed",
                            "text": str(error),
                            "tool": _MODEL_TOOL_NAME,
                        },
                    },
                )
            raise
        result = model_run.backtest
        self._chat.record_tool_result(
            request.workspace_id,
            job.tool_job_id,
            result={
                **result.as_dict(),
                "researchModelReference": model_run.model_reference.key,
                "indicatorReferences": [
                    reference.key for reference in model_run.indicator_references
                ],
                "workspaceArtifact": model_run.workspace_artifact,
                "fittedModelReference": (
                    None
                    if model_run.fitted_reference is None
                    else model_run.fitted_reference.key
                ),
            },
            actor_id=_WORKFLOW_ACTOR,
            success=result.succeeded,
            error_message=(
                None if result.succeeded else f"state {result.state!r} ({label})"
            ),
            intent_epoch=state.intent_epoch,
        )
        if active is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_PROGRESS,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "kind": "tool",
                    "toolResult": {
                        "callId": job.tool_job_id,
                        "failure": None,
                        "ok": result.succeeded,
                        "structured": result.as_dict(),
                        "summary": "Falsification execution completed",
                        "text": "",
                        "tool": _MODEL_TOOL_NAME,
                    },
                },
            )
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), replace(state)) from fence
        return model_run

    def _backtest(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        bundle: StrategyBundle,
        correlation_id: str,
        label: str,
    ) -> BacktestRunResult:
        experiment_id = self._experiment_id or f"experiment-{request.execution_id}"
        job = self._chat.submit_tool_job(
            request.workspace_id,
            request.execution_id,
            tool_name=_TOOL_NAME,
            actor_id=_WORKFLOW_ACTOR,
            correlation_id=correlation_id,
            strategy_ids=(bundle.strategy_id,),
        )
        self._chat.start_tool_job(
            request.workspace_id, job.tool_job_id, actor_id=_WORKFLOW_ACTOR
        )
        try:
            result = self._runner.run(
                BacktestRunRequest(
                    run_id=job.correlation_id,
                    workspace_id=request.workspace_id,
                    bundle=bundle,
                    workflow_execution_id=request.execution_id,
                    experiment_id=experiment_id,
                )
            )
        except Exception as error:
            self._chat.record_tool_result(
                request.workspace_id,
                job.tool_job_id,
                result=None,
                actor_id=_WORKFLOW_ACTOR,
                success=False,
                error_message=str(error),
                intent_epoch=state.intent_epoch,
            )
            raise
        self._chat.record_tool_result(
            request.workspace_id,
            job.tool_job_id,
            result=result.as_dict(),
            actor_id=_WORKFLOW_ACTOR,
            success=result.succeeded,
            error_message=None
            if result.succeeded
            else f"state {result.state!r} ({label})",
            intent_epoch=state.intent_epoch,
        )
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), replace(state)) from fence
        return result

    # -- projection and durability -----------------------------------------

    def _project_hypothesis(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        status: str | None = None,
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        if document is None:
            return state
        projected_status = (
            document.status if status is None else HypothesisStatus(status)
        )
        links = HypothesisLinks(
            runs=tuple(trial.run_id for trial in state.trials),
            evidence=tuple(trial.digest for trial in state.trials),
        )
        projected = document.projected(status=projected_status, links=links)
        self._store.save(projected)
        return replace(state, hypothesis=projected)

    def _append_attempt(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        kind: AttemptKind,
        description: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._ledger is None:
            return
        contract_sha256 = (
            state.hypothesis.content_sha256
            if state.hypothesis is not None
            else content_hash({"id": _WORKFLOW_ID, "version": _WORKFLOW_VERSION})
        )
        sequence = len(self._ledger.entries()) + 1
        self._ledger.append(
            AttemptRecord(
                attempt_id=f"attempt-{request.execution_id}-{sequence}",
                contract_sha256=contract_sha256,
                kind=kind,
                description=description,
                actor=_WORKFLOW_ACTOR,
                observed_at=_iso(self._clock()),
                payload=dict(payload),
            )
        )

    def _journal_entry(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> str | None:
        if self._journal is None:
            return None
        definition = self.definition
        journal_id = f"workflow-{request.execution_id}"
        artifacts: tuple[ArtifactRef, ...] = ()
        if state.hypothesis is not None:
            artifacts = (
                ArtifactRef.from_bytes(
                    state.hypothesis.relative_path,
                    state.hypothesis.to_bytes(),
                    media_type=_HYPOTHESIS_MEDIA_TYPE,
                ),
            )
        self._journal.accept(
            "workflow.hypothesis_development.completed",
            {
                "executionId": request.execution_id,
                "requestedBy": request.requested_by,
                "strategyIds": []
                if state.strategy_bundle is None
                else [state.strategy_bundle.strategy_id],
                "triggerMessageId": request.trigger_message_id,
                "workflow": {
                    "id": definition.workflow_id,
                    "version": definition.version,
                },
            },
            {
                "phase": _COMPLETED.value,
                "verdict": None if state.verdict is None else state.verdict.as_dict(),
            },
            workflow_hash=content_hash(
                {"id": definition.workflow_id, "version": definition.version}
            ),
            model_hash=content_hash(dict(state.model_provenance or {})),
            artifacts=artifacts,
            idempotency_id=journal_id,
        )
        return journal_id

    def _completion_payload(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"state": _COMPLETED.value}
        if state.hypothesis is not None:
            payload["hypothesis"] = {
                "name": state.hypothesis.relative_path,
                "sha256": state.hypothesis.content_sha256,
            }
        if state.verdict is not None:
            payload["verdict"] = state.verdict.outcome
        return payload

    # -- sub-agent orchestration -------------------------------------------

    def _resolve_role(self, family: str) -> Role:
        if family == "critique":
            return self._critique_role
        if family == "coding":
            return self._coding_role
        return self._research_role

    def _emit_orchestrator_progress(
        self, request: HypothesisDevelopmentRequest, text: str
    ) -> None:
        """Record a safe activity label, never the raw model instruction."""
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or not text.strip():
            return
        activity = {
            _HYPOTHESIS_CREATION: "Finalizing the structured hypothesis model.",
            _FALSIFICATION_TRIALS: "Preparing historical falsification trials.",
            _VERDICT: "Recording the falsification verdict.",
            _STRATEGY_DESIGN: "Preparing the strategy design from the surviving model.",
            _STRATEGY_BACKTESTING: "Testing the designed strategy through the harness.",
            _STRATEGY_RCA: "Diagnosing the failed strategy backtest.",
            _OPTIMIZATION: "Preparing the bounded strategy optimization run.",
        }.get(active.phase, "Preparing the next workflow step.")
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=active.phase in {_HYPOTHESIS_CREATION, _VERDICT},
            extra={"kind": "activity", "text": activity},
        )

    def _emit_orchestrator_output(
        self, request: HypothesisDevelopmentRequest, invocation: InvocationResult
    ) -> None:
        """Record the active phase orchestrator's reply as a visible artifact."""
        active = self._active_orchestrators.get(request.execution_id)
        output = invocation.output
        if active is None or not output:
            return
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=active.phase in {_HYPOTHESIS_CREATION, _VERDICT},
            extra={"artifact": {"content": output, "type": "text"}, "kind": "artifact"},
        )

    def _append_sub_agent(
        self,
        request: HypothesisDevelopmentRequest,
        event_type: ExecutionEventType,
        *,
        sub_agent_id: str,
        parent_sub_agent_id: str | None,
        phase: HypothesisDevelopmentPhase | str,
        role: str,
        context: SubAgentContextSpec,
        accepts_input: bool = False,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        append_sub_agent_event(
            self._chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            event_type=event_type,
            occurred_at=self._clock(),
            sub_agent_id=sub_agent_id,
            parent_sub_agent_id=parent_sub_agent_id,
            phase=(
                phase.value if isinstance(phase, HypothesisDevelopmentPhase) else phase
            ),
            role=role,
            context=context,
            accepts_input=accepts_input,
            intent_epoch=request.intent_epoch,
            extra=extra,
        )

    def _spawn_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        phase: HypothesisDevelopmentPhase,
    ) -> None:
        family, label = _PHASE_ORCHESTRATORS[phase]
        tools = self._subagent_mcp_tools.get(family, ())
        context = SubAgentContextSpec(
            knowledge_cutoff=None, mcp_enabled=bool(tools), mcp_tools=tools
        )
        sub_agent_id = self._id_factory("subagent")
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_SPAWNED,
            sub_agent_id=sub_agent_id,
            parent_sub_agent_id=None,
            phase=phase,
            role=label,
            context=context,
            accepts_input=phase in {_HYPOTHESIS_CREATION, _VERDICT},
        )
        self._active_orchestrators[request.execution_id] = _ActiveSubAgent(
            sub_agent_id=sub_agent_id,
            phase=phase,
            role=label,
            context=context,
        )

    def _activate_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        phase: HypothesisDevelopmentPhase,
    ) -> _ActiveSubAgent:
        """Reopen one phase's durable conversation node, creating it once."""
        for event in reversed(
            self._chat.replay_events(request.workspace_id, request.execution_id).events
        ):
            if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
                continue
            payload = event.payload
            if (
                payload.get("phase") != phase.value
                or payload.get("parentSubAgentId") is not None
            ):
                continue
            family, default_role = _PHASE_ORCHESTRATORS[phase]
            tools = self._subagent_mcp_tools.get(family, ())
            active = _ActiveSubAgent(
                sub_agent_id=str(payload["subAgentId"]),
                phase=phase,
                role=str(payload.get("role") or default_role),
                context=SubAgentContextSpec(
                    knowledge_cutoff=None,
                    mcp_enabled=bool(tools),
                    mcp_tools=tools,
                ),
            )
            self._active_orchestrators[request.execution_id] = active
            return active
        self._spawn_orchestrator(request, phase)
        return self._active_orchestrators[request.execution_id]

    def _finish_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        status: str,
        detail: str | None = None,
        summary: str | None = None,
        artifact: Mapping[str, object] | None = None,
    ) -> None:
        active = self._active_orchestrators.pop(request.execution_id, None)
        if active is None:
            return
        if summary:
            artifact_payload: dict[str, object] = (
                dict(artifact)
                if artifact is not None
                else {"content": summary, "type": "text"}
            )
            if "content" not in artifact_payload:
                artifact_payload["content"] = summary
            if "type" not in artifact_payload:
                artifact_payload["type"] = "text"
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_OUTPUT,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "artifact": artifact_payload,
                    "kind": "artifact",
                },
            )
        extra: dict[str, object] = {"frozen": True, "status": status}
        if detail is not None:
            extra["detail"] = detail
        event_type = (
            ExecutionEventType.SUB_AGENT_COMPLETED
            if status == STATUS_COMPLETED
            else ExecutionEventType.SUB_AGENT_FAILED
        )
        self._append_sub_agent(
            request,
            event_type,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=False,
            extra=extra,
        )

    def _phase_summary(
        self, state: HypothesisDevelopmentState, phase: HypothesisDevelopmentPhase
    ) -> str:
        """Summarize what one phase produced, for its transcript."""
        if phase is _HYPOTHESIS_CREATION:
            if state.hypothesis is not None:
                return f"Hypothesis: {state.hypothesis.front_matter.claim}"
            if state.draft is not None:
                return f"Drafted hypothesis: {state.draft.claim}"
        elif phase is _FALSIFICATION_TRIALS:
            return f"Ran {len(state.trials)} falsification trial(s)."
        elif phase is _VERDICT and state.verdict is not None:
            return f"Verdict: {state.verdict.outcome}. {state.verdict.reason}".strip()
        elif phase is _STRATEGY_DESIGN and state.strategy_bundle is not None:
            bundle = state.strategy_bundle
            return (
                f"Designed strategy {bundle.strategy_id} {bundle.version} "
                f"(sha256 {bundle.bundle_sha256[:12]}â€¦)."
            )
        elif phase is _STRATEGY_BACKTESTING and state.strategy_backtest is not None:
            return (
                f"Strategy backtest {state.strategy_backtest.job_id} ended in "
                f"state {state.strategy_backtest.state}."
            )
        elif phase is _STRATEGY_RCA:
            return f"Recorded {len(state.rca)} strategy RCA diagnosis(es)."
        elif phase is _OPTIMIZATION and state.optimization_backtest is not None:
            return "Optimization backtest completed."
        return f"{phase.value.capitalize()} phase completed."

    def _on_phase_entered(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> None:
        active = self._active_orchestrators.get(request.execution_id)
        if (
            active is not None
            and active.phase is _FALSIFICATION_TRIALS
            and state.trials
        ):
            report = self._falsification_report_artifact(
                state, prose=state.falsification_prose or ""
            )
            self._finish_orchestrator(
                request,
                status=STATUS_COMPLETED,
                summary=self._phase_summary(state, active.phase),
                artifact=report,
            )
        else:
            summary = (
                None if active is None else self._phase_summary(state, active.phase)
            )
            self._finish_orchestrator(request, status=STATUS_COMPLETED, summary=summary)
        if state.phase in _ACTIVE_PHASES:
            self._activate_orchestrator(request, state.phase)
            task = _PHASE_TASKS.get(state.phase)
            if task is not None:
                self._emit_orchestrator_progress(request, task)

    def _author_falsification_prose(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
    ) -> str:
        """Prompt the sub-agent for the falsification phase to produce its prose analysis."""
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-falsification-prose",
        )
        task = _falsification_turn_prompt(
            state,
            (state.fitted_parameters or {}),
        )
        try:
            invocation = self._router.invoke(
                self._research_role,
                prompt_for(task),
            )
            return (invocation.output or "").strip()
        except Exception:
            return ""

    def _falsification_report_artifact(
        self, state: HypothesisDevelopmentState, prose: str = ""
    ) -> dict[str, object]:
        """Build the rich structured falsification trials report artifact."""
        tests_by_id = {t.test_id: t for t in (state.plan.tests if state.plan else ())}
        passed_count = sum(
            1 for t in state.trials if not t.falsified and t.observed is not None
        )
        falsified_count = sum(1 for t in state.trials if t.falsified)
        total_count = len(state.trials)
        survived = (
            total_count > 0 and falsified_count == 0 and passed_count == total_count
        )

        trials_data: list[dict[str, object]] = []
        for t in state.trials:
            planned = tests_by_id.get(t.test_id)
            trials_data.append(
                {
                    "testId": t.test_id,
                    "falsifierId": t.falsifier_id,
                    "runId": t.run_id,
                    "runState": t.run_state,
                    "detail": t.detail,
                    "observed": t.observed,
                    "falsified": t.falsified,
                    "observable": (planned.observable if planned else t.falsifier_id),
                    "condition": planned.condition if planned else "",
                    "threshold": planned.threshold if planned else 0.0,
                    "description": planned.description if planned else "",
                }
            )

        controls_data = [
            {
                "controlId": c.control_id,
                "description": c.description,
                "expectation": c.expectation,
            }
            for c in (state.plan.controls if state.plan else ())
        ]

        summary_text = (
            f"Ran {total_count} falsification trial(s): {passed_count} survived, {falsified_count} refuted."
            if total_count > 0
            else "No falsification trials were executed."
        )

        metrics_data: dict[str, object] = {}
        for name, val in (state.fitted_parameters or {}).items():
            if isinstance(val, (int, float)) and math.isfinite(float(val)):
                metrics_data[name] = float(val)
            else:
                metrics_data[name] = str(val)

        return {
            "type": "falsification_report",
            "content": summary_text,
            "prose": prose,
            "passedCount": passed_count,
            "falsifiedCount": falsified_count,
            "totalCount": total_count,
            "survived": survived,
            "hypothesis": {
                "claim": (
                    state.hypothesis.front_matter.claim if state.hypothesis else ""
                ),
                "disagreement": (
                    state.hypothesis.front_matter.disagreement
                    if state.hypothesis
                    else ""
                ),
                "universe": (
                    list(state.hypothesis.front_matter.universe)
                    if state.hypothesis
                    else []
                ),
                "horizon": (
                    state.hypothesis.front_matter.horizon if state.hypothesis else ""
                ),
            },
            "trials": trials_data,
            "controls": controls_data,
            "metrics": metrics_data,
        }

    def _request_operator_input(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        purpose: str,
        detail: str,
        active: _ActiveSubAgent | None = None,
    ) -> NoReturn:
        """Surface an LLM-authored operator request via the orchestrator node.

        Operator-facing text is never a canned workflow string. The model is
        asked to author the request from the concrete reason the workflow
        paused; the only fallback is the raw reason itself, used solely when the
        model is unavailable so the workflow still fails loudly rather than
        inventing conversational text.
        """
        resolved = active or self._active_orchestrators.get(request.execution_id)
        question = self._operator_question(
            request,
            state,
            active=resolved,
            purpose=purpose,
            detail=detail,
        )
        self._emit_orchestrator_text(request, question)
        if resolved is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
                sub_agent_id=resolved.sub_agent_id,
                parent_sub_agent_id=None,
                phase=resolved.phase,
                role=resolved.role,
                context=resolved.context,
                accepts_input=True,
                extra={"question": question},
            )
        raise _Fenced(question, state)

    def _operator_question(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        active: _ActiveSubAgent | None,
        purpose: str,
        detail: str,
    ) -> str:
        """Ask the model to author one operator-facing request.

        Returns the raw reason when the model is unavailable, so the operator
        still sees the concrete failure instead of a fabricated prompt.
        """
        del state
        if active is None:
            return detail
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-operator-question",
        )
        task = "\n".join(
            [
                "You are the workflow's operator-facing voice. The workflow has "
                "paused and needs the operator's direction to continue.",
                "Author the operator request, not a summary.",
                f"Purpose: {purpose}",
                f"Concrete reason the workflow paused: {detail}",
                "",
                "Write exactly one concise, natural request addressed to the "
                "operator. Include the concrete reason, say what you need from "
                "them, and do not mention internal step names, schemas, or that "
                "you are a model. Reply with the request text only: no quotes, "
                "no JSON, no code fences.",
            ]
        )
        try:
            invocation = self._router.invoke(self._research_role, prompt_for(task))
        except Exception:  # noqa: BLE001 - a failed author falls back to the reason
            return detail
        text = (invocation.output or "").strip()
        return text or detail

    def _transition_checked(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
        check: Callable[[], str | None],
    ) -> HypothesisDevelopmentState:
        """Validate phase exit criteria, requesting operator input conversationally if blocked."""
        failure = check()
        if failure is not None:
            return self._request_operator_input(
                request,
                state,
                purpose=f"validate the step before {target.value}",
                detail=failure,
            )
        return self._transition(request, state, target, reason)

    def _transition(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
    ) -> HypothesisDevelopmentState:
        advanced = self._advance(state, target, reason)
        self._emit_phase(request, target, {"reason": reason})
        try:
            self._chat.set_execution_phase(
                request.workspace_id, request.execution_id, target.value
            )
        except Exception:  # noqa: BLE001 - phase is best-effort projection state
            pass
        self._on_phase_entered(request, advanced)
        return advanced

    def _advance(
        self,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
    ) -> HypothesisDevelopmentState:
        transition_phase(state.phase, target)
        transition = PhaseTransition(
            sequence=len(state.transitions) + 1,
            from_phase=state.phase,
            to_phase=target,
            at=self._clock(),
            intent_epoch=state.intent_epoch,
            reason=reason,
        )
        return replace(
            state, phase=target, transitions=(*state.transitions, transition)
        )

    def _emit_phase(
        self,
        request: HypothesisDevelopmentRequest,
        phase: HypothesisDevelopmentPhase,
        payload: Mapping[str, object],
    ) -> None:
        self._chat.event_store.append(
            ExecutionEvent(
                execution_id=request.execution_id,
                workspace_id=request.workspace_id,
                event_type=ExecutionEventType.WORKFLOW_PHASE,
                correlation_id=request.execution_id,
                actor_id=_WORKFLOW_ACTOR,
                actor_scope=ActorScope.WORKFLOW,
                occurred_at=self._clock(),
                payload={"phase": phase.value, **dict(payload)},
                intent_epoch=request.intent_epoch,
                event_id=self._id_factory("event"),
            )
        )

    def _pause_for_compose(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        error: FalsificationComposeError,
    ) -> HypothesisDevelopmentState:
        """Pause for operator input when structured output cannot be composed.

        A model-format problem is an operator conversation, not a run failure:
        the open orchestrator is marked as needing input with a concrete
        question, and the execution moves to ``paused`` so it can be resumed.
        """
        active = self._active_orchestrators.get(request.execution_id)
        question = self._operator_question(
            request,
            state,
            active=active,
            purpose="recover from unparseable structured model output",
            detail=str(error),
        )
        if active is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=True,
                extra={
                    "kind": "compose",
                    "question": question,
                    "reason": str(error),
                },
            )
        return self._pause(request, state, _Fenced(f"{question} ({error})", state))

    def _pause(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        fence: _Fenced,
    ) -> HypothesisDevelopmentState:
        detail = str(fence)
        if state.phase in {_COMPLETED, _FAILED}:
            return replace(state, detail=detail, fenced=True)
        self._paused_states[request.execution_id] = state
        try:
            paused = self._advance(state, _PAUSED, detail)
        except InvalidPhaseTransition:
            return replace(state, detail=detail, fenced=True)
        self._pause_execution(request)
        return replace(paused, detail=detail, fenced=True)

    def _pause_execution(self, request: HypothesisDevelopmentRequest) -> None:
        """Move a running execution to ``paused`` without ending it."""
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return
        if execution.state is not WorkflowExecutionState.RUNNING:
            return
        self._chat.pause_workflow(
            request.workspace_id,
            request.execution_id,
            actor_id=request.requested_by,
        )

    def _fail(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        error: Exception,
    ) -> HypothesisDevelopmentState:
        if self._execution_cancelled_or_deleted(request):
            return replace(
                state, error_message=str(error), detail=str(error), fenced=True
            )
        self._finish_orchestrator(
            request,
            status=STATUS_FAILED,
            detail=str(error),
            summary=f"Phase failed: {error}",
        )
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": str(error), "state": "failed"},
        )
        failed = self._advance(state, _FAILED, f"workflow failed: {error}")
        return replace(failed, error_message=str(error), detail=str(error))

    def _outcome(
        self, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentOutcome:
        return HypothesisDevelopmentOutcome(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            phase=state.phase,
            fenced=state.fenced,
            transitions=state.transitions,
            hypothesis=state.hypothesis,
            research_model_reference=state.research_model_reference,
            research_model_artifact=state.research_model_artifact,
            fitted_model_reference=state.fitted_model_reference,
            fitted_parameters=state.fitted_parameters,
            research_model_runtime_configuration=state.research_model_runtime_configuration,
            plan=state.plan,
            trials=state.trials,
            rca=state.rca,
            verdict=state.verdict,
            strategy_bundle=state.strategy_bundle,
            exit_criteria=state.exit_criteria,
            hypothesis_cycles=state.hypothesis_cycles,
            strategy_cycles=state.strategy_cycles,
            strategy_backtest=state.strategy_backtest,
            optimization_backtest=state.optimization_backtest,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _replay_transitions(
        self, request: HypothesisDevelopmentRequest
    ) -> tuple[PhaseTransition, ...]:
        """Rebuild phase history when a paused conversation resumes."""
        transitions: list[PhaseTransition] = []
        previous = _REQUESTED
        for event in self._chat.replay_events(
            request.workspace_id, request.execution_id
        ).events:
            if event.event_type is not ExecutionEventType.WORKFLOW_PHASE:
                continue
            raw_phase = event.payload.get("phase")
            try:
                target = HypothesisDevelopmentPhase(str(raw_phase))
            except ValueError:
                continue
            if target in {_PAUSED, _FAILED} or target is previous:
                continue
            transitions.append(
                PhaseTransition(
                    sequence=len(transitions) + 1,
                    from_phase=previous,
                    to_phase=target,
                    at=event.occurred_at,
                    intent_epoch=event.intent_epoch,
                    reason=str(
                        event.payload.get("reason") or "durable phase transition"
                    ),
                )
            )
            previous = target
        return tuple(transitions)

    def _require_execution(
        self, request: HypothesisDevelopmentRequest
    ) -> WorkflowExecution:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise WorkflowError(
                f"execution {request.execution_id!r} does not exist"
            ) from error
        if execution.chat_id != request.chat_id:
            raise WorkflowError("execution does not belong to the request chat")
        if execution.trigger_message_id != request.trigger_message_id:
            raise WorkflowError("trigger message does not match the execution")
        return execution

    def _execution_cancelled_or_deleted(
        self, request: HypothesisDevelopmentRequest
    ) -> bool:
        """Return whether deletion or cancellation invalidated this worker."""
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return True
        return execution.state in {
            WorkflowExecutionState.STOPPED,
            WorkflowExecutionState.CANCELLED,
        }

    def _check_stale_request(self, request: HypothesisDevelopmentRequest) -> None:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise _Fenced("execution was deleted") from error
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise WorkflowError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> None:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise _Fenced("execution was deleted") from error
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise _Fenced(f"execution became {execution.state.value}")
        if execution.intent_epoch != state.intent_epoch:
            raise _Fenced("steering advanced the intent epoch")

    def _finish_execution(
        self,
        request: HypothesisDevelopmentRequest,
        final_state: WorkflowExecutionState,
        event_type: ExecutionEventType,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return
        if execution.state in _TERMINAL_EXECUTION_STATES:
            return
        now = self._clock()
        updated = self._chat.persistence.save(
            replace_record(
                execution, state=final_state, finished_at=now, updated_at=now
            )
        )
        self._chat.event_store.append(
            ExecutionEvent(
                execution_id=updated.execution_id,
                workspace_id=updated.workspace_id,
                event_type=event_type,
                correlation_id=updated.execution_id,
                actor_id=_WORKFLOW_ACTOR,
                actor_scope=ActorScope.WORKFLOW,
                occurred_at=now,
                payload=dict(payload or {"state": final_state.value}),
                intent_epoch=updated.intent_epoch,
                event_id=self._id_factory("event"),
            )
        )
