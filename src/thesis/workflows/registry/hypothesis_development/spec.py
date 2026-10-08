"""Assemble the hypothesis-development workflow specification and object.

The spec is the registered definition of the concrete workflow: its ordered
phases, the allowed transition table, and the typed piping between phases.
Every phase is declared as a sealed-union phase object — conversation,
one-shot, or deterministic — bound to the module that owns its behavior; the
state machine wiring lives here instead of inside a workflow class.  The
builder then assembles the one general
:class:`~thesis.workflows.core.workflow.Workflow` instance from that spec, the
doctrine, the services bundle, and the ports.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from types import MappingProxyType
from typing import Any, Literal, cast

from thesis.agent.router import ModelRouter, Role
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.schema import render_violations, validate_arguments
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.service import ChatService
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence.journal import CommandJournal
from thesis.research.hypothesis import HypothesisStore, InMemoryHypothesisStore
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import BacktestRunner
from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.execution_store import ExecutionStore
from thesis.workflows.core.phase import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
    PhaseSpec,
    TerminalPhase,
)
from thesis.workflows.core.spec import HandoffContext, PipingFunction, WorkflowSpec
from thesis.workflows.core.workflow import Detach, Workflow, WorkflowPorts
from thesis.workflows.research_models import (
    PROGRAM_REFERENCE_SCHEMA,
    BacktestResearchModelRunner,
    ProgramSnapshots,
    ResearchModelRunner,
)

from .contracts.creation import HypothesisDevelopmentRequest
from .contracts.discovery import DiscoveryDraft
from .contracts.validation import (
    DISCOVERY_COMPLETION_SCHEMA,
    HYPOTHESIS_COMPLETION_SCHEMA,
    discovery_payload_errors,
    format_contract_errors,
    hypothesis_payload_errors,
)
from .errors import FalsificationComposeError, WorkflowError
from .phases import (
    _FAILED,
    _VERDICT,
    HypothesisDevelopmentPhase,
)
from .phases.trials import _draft_from_contract, run_falsification_trials
from .piping import (
    identity_pipe,
    reset_backtests,
    reset_discovery,
    reset_premise,
)
from .primitives import (
    _RCA_SCHEMA,
    _new_id,
    _utc_now,
)
from .rebuild import (
    PayloadFold,
    _fold_discovery,
    _fold_draft,
    _fold_trials,
    _fold_verdict,
)
from .services import (
    DevelopmentEngine,
    HypothesisDevelopmentServices,
)
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

__all__ = [
    "build_hypothesis_development_spec",
    "build_hypothesis_development_workflow",
    "piping",
]


def _start_handoff(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    context: HandoffContext,
) -> str | None:
    """Create the first phase's prompt from the operator's launching message.

    The workflow starts in discovery, so the start adapter builds the discovery
    prompt around the caller's message, verbatim.  A non-start handoff returns
    ``None`` so each later phase keeps its declared seed.
    """
    if context.source is not None:
        return None
    return _discovery_prompt(workflow, request)


def _discovery_prompt(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
) -> str:
    """Build the discovery prompt: phase instructions plus the operator's words."""
    from .phases.creation import trigger_text
    from .prompts.discovery import _discovery_turn_prompt

    return _discovery_turn_prompt([("user", trigger_text(workflow, request))])


def _discovery_seed(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the discovery agent with its instructions or a restart brief.

    The workflow's first entry is a fresh start: the full discovery prompt
    carries the operator's launching message.  A verdict restart re-enters the
    same durable conversation, so its brief carries only the verdict that
    superseded the previous candidate.
    """
    del state
    if context is None or context.source is None:
        return _discovery_prompt(workflow, request)
    from .prompts.discovery import _discovery_restart_section

    return _discovery_restart_section(
        dict(context.completion) if context.completion else None
    )


def _discovery_handoff(
    draft: DiscoveryDraft | None,
) -> dict[str, object] | None:
    """Render the accepted candidate compactly for the model-creation seed."""
    if draft is None:
        return None
    return {
        "candidate": draft.candidate.as_dict(),
        "attempts": [
            f"{attempt.purpose}: {attempt.result}" for attempt in draft.attempts
        ],
        "untouchedData": draft.untouched_data,
        "limitations": draft.limitations,
    }


def _accepted_discovery(
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> DiscoveryDraft | None:
    """Return the selected candidate from state or the accepted handoff.

    The live pipe keeps an already-selected draft, but the first
    discovery→model-creation transition carries the candidate in the accepted
    completion rather than the pre-transition state; both paths seed the same
    handoff.
    """
    if state.discovery is not None:
        return state.discovery
    payload = context.completion.get("discovery") if context else None
    if not isinstance(payload, Mapping) or not payload:
        return None
    try:
        return DiscoveryDraft.from_dict(payload)
    except WorkflowError:
        return None


def _model_creation_seed(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the model-creation agent with its instructions and the candidate.

    A fresh entry hands over the full model-creation instructions; a verdict
    circle-back re-enters the same durable conversation with the compact
    revision brief that points back at the recorded trial findings.  Both
    entries carry the still-selected discovery candidate when the state or the
    accepted handoff holds one, because model creation must model exactly that
    candidate.
    """
    from .prompts.model_creation import (
        _model_creation_instructions,
        _model_creation_revision_section,
    )

    if context is not None and context.source == _VERDICT.value:
        report = _latest_phase_artifact(workflow, request, "falsification_trials")
        parts = [
            _model_creation_revision_section(
                dict(context.completion) if context.completion else None,
                report,
            )
        ]
    else:
        parts = [_model_creation_instructions()]
        if context is not None and context.completion:
            parts += [
                "",
                f"Accepted completion from the {context.source} phase:",
                json.dumps(
                    dict(context.completion),
                    ensure_ascii=True,
                    indent=2,
                    default=str,
                ),
            ]
    handoff = _discovery_handoff(_accepted_discovery(state, context))
    if handoff is not None:
        parts += [
            "",
            "Selected discovery candidate:",
            json.dumps(handoff, ensure_ascii=True, indent=2, default=str),
        ]
    return "\n".join(parts)


def _latest_phase_artifact(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    phase: str,
) -> Mapping[str, object] | None:
    """Return the latest recorded artifact for one phase, if any.

    The recorded report outlives the state reset a loop-back applies, so the
    revision seed reads it from the durable execution events instead of the
    cleared typed state.
    """
    from thesis.execution.events import ExecutionEventType

    for event in reversed(
        workflow.observe(request.workspace_id, request.execution_id).events
    ):
        if event.event_type is not ExecutionEventType.WORKFLOW_PHASE_RESULT:
            continue
        payload = event.payload
        if payload.get("phase") != phase:
            continue
        artifact = payload.get("artifact")
        if isinstance(artifact, Mapping):
            return dict(artifact)
    return None


def _strategy_design_seed(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the design agent with the surviving hypothesis and its ask protocol.

    The agent must explain the strategy it designs in prose and ask the
    operator to confirm it before the workflow advances to backtesting.
    """
    del workflow, request
    from .prompts.strategy_design import _strategy_design_instructions

    parts = [_strategy_design_instructions()]
    hypothesis = _surviving_hypothesis(state)
    if hypothesis is not None:
        parts += [
            "",
            "Surviving hypothesis:",
            json.dumps(hypothesis, ensure_ascii=True, indent=2, default=str),
        ]
    if context is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _surviving_hypothesis(
    state: HypothesisDevelopmentState,
) -> dict[str, object] | None:
    """Return the claim and falsifiers the strategy must serve, if recorded."""
    draft = state.draft
    if draft is None:
        return None
    return {
        "claim": draft.claim,
        "disagreement": draft.disagreement,
        "riskFactor": draft.risk_factor,
        "horizon": draft.horizon,
        "universe": list(draft.universe),
        "falsifiers": [
            {
                "falsifierId": falsifier.falsifier_id,
                "observable": falsifier.observable,
                "condition": falsifier.condition,
                "threshold": falsifier.threshold,
                "test": falsifier.test,
            }
            for falsifier in draft.falsifiers
        ],
    }


def _verdict_seed(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    context: HandoffContext,
) -> str:
    """Seed the verdict agent with the ask protocol and the recorded trials.

    The deterministic trials land as the accepted completion from the
    falsification-trials phase; the verdict agent proposes the verdict from
    them and must ask the operator through ``request_workflow_input`` before
    completing — it never auto-completes over the trials.
    """
    del workflow, request, state
    from .prompts.verdict import _verdict_instructions

    parts = [_verdict_instructions()]
    if context.source is not None and context.completion:
        parts += [
            "",
            f"Accepted completion from the {context.source} phase:",
            json.dumps(
                dict(context.completion), ensure_ascii=True, indent=2, default=str
            ),
        ]
    return "\n".join(parts)


def _contract_seed(task: str, *, schema: str | None = None) -> Callable[..., str]:
    """Build a seed that carries the accepted handoff and the contract shape."""

    def seed(
        workflow: Workflow[Any, Any, Any],
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        context: HandoffContext,
    ) -> str:
        del workflow, request, state
        parts = [task]
        if context.source is not None and context.completion:
            parts += [
                "",
                f"Accepted completion from the {context.source} phase:",
                json.dumps(
                    dict(context.completion), ensure_ascii=True, indent=2, default=str
                ),
            ]
        if schema is not None:
            parts += ["", f"Required shape: {schema}"]
        return "\n".join(parts)

    return seed


def _mapping_field(name: str) -> Callable[[Mapping[str, object]], str | None]:
    """Reject a completion whose ``name`` field is not a non-empty object."""

    def validate(completion: Mapping[str, object]) -> str | None:
        value = completion.get(name)
        if not isinstance(value, Mapping) or not value:
            return f"completion field {name!r} must be a non-empty object"
        return None

    return validate


def _list_field(name: str) -> Callable[[Mapping[str, object]], str | None]:
    """Reject a completion whose ``name`` field is not a non-empty list."""

    def validate(completion: Mapping[str, object]) -> str | None:
        value = completion.get(name)
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or not value
        ):
            return f"completion field {name!r} must be a non-empty list"
        return None

    return validate


def _discovery_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the discovery contract before any transition commits.

    Every missing or wrongly-typed candidate or attempt field is reported at
    once, so the agent repairs the whole contract in one turn, and the typed
    :class:`DiscoveryDraft` decoder re-runs as a drift guard: anything the
    collector missed still rejects the contract here rather than later in the
    rebuild fold.
    """
    payload = completion.get("discovery")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'discovery' must be a non-empty object"
    errors = discovery_payload_errors(payload)
    if not errors:
        try:
            DiscoveryDraft.from_dict(payload)
        except WorkflowError as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _creation_validator(completion: Mapping[str, object]) -> str | None:
    """Deeply validate the creation contract before any transition commits.

    The creation handoff is the one contract a deterministic phase consumes,
    so the payload is validated completely here — inside the engine's
    pre-transition validator — instead of first failing inside the trials
    phase after the state has already moved.  Every missing or wrongly-typed
    field is reported at once so the agent repairs the whole contract in one
    turn.
    """
    payload = completion.get("hypothesis")
    if not isinstance(payload, Mapping) or not payload:
        return "completion field 'hypothesis' must be a non-empty object"
    errors = hypothesis_payload_errors(payload)
    if not errors:
        # Drift guard: the collector must be at least as strict as the
        # composer the deterministic successor phase runs, so anything the
        # collector missed still rejects the contract here — never later,
        # after the transition has committed.
        try:
            _draft_from_contract(payload)
        except (FalsificationComposeError, WorkflowError) as error:
            errors = [str(error)]
    if errors:
        return format_contract_errors(errors)
    return None


def _strategy_receipt_validator(completion: Mapping[str, object]) -> str | None:
    """Check receipt structure; composition verifies its persisted provenance."""
    receipt = completion.get("strategy")
    if not isinstance(receipt, Mapping):
        return "completion field 'strategy' must be a strategy authoring receipt"
    misses = validate_arguments(PROGRAM_REFERENCE_SCHEMA, receipt)
    if misses:
        return "invalid strategy authoring receipt: " + render_violations(misses)
    if receipt["kind"] != "strategy":
        return "strategy completion must reference a strategy program"
    return None


def _phase_completion_schema(
    phase: HypothesisDevelopmentPhase,
    required: tuple[str, ...],
    targets: tuple[str, ...],
) -> Mapping[str, object] | None:
    """Return the completion JSON Schema advertised for one phase's agent.

    Discovery and model creation carry their full deep schemas; every other
    agent phase advertises its required handoff fields and the declared target
    enum, so the provider constrains the contract at decode time instead of
    relying on prompt text alone.
    """
    if phase is HypothesisDevelopmentPhase.DISCOVERY:
        return DISCOVERY_COMPLETION_SCHEMA
    if phase is HypothesisDevelopmentPhase.MODEL_CREATION:
        return HYPOTHESIS_COMPLETION_SCHEMA
    if phase is HypothesisDevelopmentPhase.STRATEGY_DESIGN:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["strategy"],
            "properties": {
                "target": {"type": "string", "enum": ["strategy_backtesting"]},
                "strategy": {
                    **PROGRAM_REFERENCE_SCHEMA,
                    "properties": {
                        **cast(
                            dict[str, object], PROGRAM_REFERENCE_SCHEMA["properties"]
                        ),
                        "kind": {"type": "string", "enum": ["strategy"]},
                    },
                },
            },
        }
    # The advertised target enum names only real successors: the engine still
    # accepts a terminal failure target, but the advertised shape never
    # invites the agent to end the whole run itself.
    targets = tuple(target for target in targets if target != _FAILED.value)
    if not required and not targets:
        return None
    properties: dict[str, object] = {
        "target": {"type": "string", "enum": targets},
    }
    for name in required:
        properties[name] = {"type": "object"}
    schema: dict[str, object] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True, slots=True)
class PhaseDeclaration:
    """One authoritative phase, executor policy, and outgoing edge declaration."""

    spec: PhaseSpec[
        DevelopmentEngine, HypothesisDevelopmentRequest, HypothesisDevelopmentState
    ]
    edges: Mapping[str, PipingFunction[HypothesisDevelopmentState]]
    orchestrator: tuple[str, str] | None
    task: str | None
    activity: str | None
    mcp_tools: tuple[str, ...]
    tools: tuple[str, ...]
    knowledge: tuple[str, ...] | None
    payload_fold: PayloadFold | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edges", MappingProxyType(dict(self.edges)))


@cache
def phase_declarations() -> tuple[PhaseDeclaration, ...]:
    """Declare every live phase; all secondary lookup maps derive from here."""
    phase = HypothesisDevelopmentPhase
    knowledge_tools = ("search_knowledge", "read_knowledge")
    program_tools = (
        tuple(
            f"{operation}_{kind}"
            for kind in ("study", "strategy", "indicator")
            for operation in ("author", "update", "read", "validate", "publish")
        )
        + tuple(f"list_{plural}" for plural in ("studies", "strategies", "indicators"))
        + ("read_sdk_reference",)
    )
    backtest_tools = (
        *knowledge_tools,
        "read_strategy",
        "run_backtest",
        "read_sdk_reference",
    )
    run_evidence_tools = (
        "get_job_status",
        "get_job_result",
        "list_artifacts",
        "read_artifact",
    )
    backtest_execution_guidance = (
        "Use the handed-off strategy.programId with read_strategy and "
        "run_backtest {program_id: ...}. Execution uses the latest committed "
        "content and requires its current accepted validation. Use read_sdk_reference "
        "for exact SDK signatures, not guessed APIs. If validation is stale or "
        "rejected, report the blocker rather than supplying inline source or "
        "bypassing validation. Launch acceptance is not a successful completed run: "
        "never assume launch success means results are available. Retain the returned "
        "job_id and inspect get_job_status {job_id: ...} while queued or "
        "running. After terminal status, call get_job_result {job_id: ...}, "
        "list_artifacts {job_id: ...} and read_artifact {job_id: ..., name: ...} to "
        "inspect actual "
        "metrics and evidence. Record terminal failures honestly; do not fabricate "
        "metrics or artifacts. Include strategy.programId and the actual executed "
        "version/gitCommit from the launch receipt in the completion handoff so the "
        "next phase can identify the latest job. Request phase completion yourself "
        "only after inspecting the terminal outcome. These are agent instructions, "
        "not runtime polling or repair caps."
    )

    def agent(
        name: HypothesisDevelopmentPhase,
        kind: type[ConversationPhase] | type[OneShotPhase],
        edges: Mapping[str, PipingFunction[HypothesisDevelopmentState]],
        family: str,
        label: str,
        task: str,
        activity: str,
        required: str,
        validator: Callable[[Mapping[str, object]], str | None],
        seed: Callable[..., str],
        payload_fold: PayloadFold | None,
        mcp_tools: tuple[str, ...] = (),
        tools: tuple[str, ...] = knowledge_tools,
        knowledge: tuple[str, ...] | None = None,
    ) -> PhaseDeclaration:
        targets = tuple(edges)
        return PhaseDeclaration(
            spec=kind(
                phase=name.value,
                validate=validator,
                required=(required,),
                seed=seed,
                completion_schema=_phase_completion_schema(name, (required,), targets),
            ),
            edges=edges,
            orchestrator=(family, label),
            task=task,
            activity=activity,
            mcp_tools=mcp_tools,
            tools=tools,
            knowledge=knowledge,
            payload_fold=payload_fold,
        )

    declarations = (
        agent(
            phase.DISCOVERY,
            ConversationPhase,
            {
                phase.MODEL_CREATION.value: reset_premise,
                phase.FAILED.value: identity_pipe,
            },
            "research",
            "Thesis discovery orchestrator",
            "Run small targeted studies on the premise, record every attempt, and identify a candidate opportunity with its signal, information timing, holding period and profit source, plus the main competing explanation; stop when nothing is worth pursuing.",
            "Running targeted studies to find a candidate opportunity.",
            "discovery",
            _discovery_validator,
            _discovery_seed,
            _fold_discovery,
            (
                "get_job_status",
                "get_job_result",
                "list_artifacts",
                "read_artifact",
            ),
            tools=(*knowledge_tools, *program_tools, "run_study"),
            knowledge=("discovery", "data-quality", "trading-costs", "model-cards"),
        ),
        agent(
            phase.MODEL_CREATION,
            ConversationPhase,
            {
                phase.FALSIFICATION_TRIALS.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "research",
            "Thesis model-creation orchestrator",
            "Turn the selected discovery candidate into ONE statistical model and pre-register the falsifier tests from the knowledge cards.",
            "Finalizing the structured hypothesis model.",
            "hypothesis",
            _creation_validator,
            _model_creation_seed,
            _fold_draft,
            tools=(*knowledge_tools, *program_tools),
            knowledge=("model-cards", "inference", "validation", "trading-costs"),
        ),
        PhaseDeclaration(
            spec=DeterministicPhase(
                phase=phase.FALSIFICATION_TRIALS.value,
                contract=run_falsification_trials,
                validate=_list_field("trials"),
                required=("trials",),
                on_complete=phase.VERDICT.value,
            ),
            edges={
                phase.VERDICT.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            orchestrator=None,
            task=None,
            activity=None,
            mcp_tools=(),
            tools=(),
            knowledge=None,
            payload_fold=_fold_trials,
        ),
        agent(
            phase.VERDICT,
            ConversationPhase,
            {
                phase.STRATEGY_DESIGN.value: identity_pipe,
                phase.MODEL_CREATION.value: reset_premise,
                phase.DISCOVERY.value: reset_discovery,
                phase.FAILED.value: identity_pipe,
            },
            "research",
            "Thesis research verdict orchestrator",
            "Record the falsification verdict from the trial results.",
            "Recording the falsification verdict.",
            "verdict",
            _mapping_field("verdict"),
            _verdict_seed,
            _fold_verdict,
        ),
        agent(
            phase.STRATEGY_DESIGN,
            ConversationPhase,
            {
                phase.STRATEGY_BACKTESTING.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis coding strategy-design orchestrator",
            "Implement a sandbox-valid strategy whose exits re-check the hypothesis falsifiers.",
            "Preparing the strategy design from the surviving model.",
            "strategy",
            _strategy_receipt_validator,
            _strategy_design_seed,
            None,
            tools=(*knowledge_tools, *program_tools),
        ),
        agent(
            phase.STRATEGY_BACKTESTING,
            OneShotPhase,
            {
                phase.OPTIMIZATION.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis strategy-backtesting orchestrator",
            "Run the designed strategy through the harness and record its test result.",
            "Testing the designed strategy through the harness.",
            "backtest",
            _mapping_field("backtest"),
            _contract_seed(
                "Run the designed strategy through the harness and record its test result. "
                + backtest_execution_guidance
            ),
            None,
            mcp_tools=run_evidence_tools,
            tools=backtest_tools,
        ),
        agent(
            phase.STRATEGY_RCA,
            OneShotPhase,
            {
                phase.STRATEGY_DESIGN.value: reset_backtests,
                phase.FAILED.value: identity_pipe,
            },
            "critique",
            "Thesis strategy root-cause orchestrator",
            "Diagnose the failed strategy backtest and prescribe a concrete revision.",
            "Diagnosing the failed strategy backtest.",
            "diagnosis",
            _mapping_field("diagnosis"),
            _contract_seed(
                "Diagnose the failed strategy backtest and prescribe a concrete revision.",
                schema=_RCA_SCHEMA,
            ),
            None,
        ),
        agent(
            phase.OPTIMIZATION,
            OneShotPhase,
            {
                phase.COMPLETED.value: identity_pipe,
                phase.STRATEGY_RCA.value: identity_pipe,
                phase.FAILED.value: identity_pipe,
            },
            "coding",
            "Thesis coding optimization orchestrator",
            "Run the optimization backtest for the designed strategy.",
            "Preparing the bounded strategy optimization run.",
            "optimization",
            _mapping_field("optimization"),
            _contract_seed(
                "Run the optimization backtest for the designed strategy. "
                + backtest_execution_guidance
            ),
            None,
            mcp_tools=run_evidence_tools,
            tools=backtest_tools,
        ),
        PhaseDeclaration(
            TerminalPhase(phase=phase.COMPLETED.value),
            {},
            None,
            None,
            None,
            (),
            (),
            None,
            None,
        ),
        PhaseDeclaration(
            TerminalPhase(phase=phase.FAILED.value),
            {},
            None,
            None,
            None,
            (),
            (),
            None,
            None,
        ),
    )
    names = [item.spec.phase for item in declarations]
    if len(set(names)) != len(names) or set(names) != {
        item.value for item in phase if item is not phase.HYPOTHESIS_CREATION
    }:
        raise WorkflowError(
            "phase declarations must cover every live phase exactly once"
        )
    return declarations


def piping() -> Mapping[tuple[str, str], PipingFunction[HypothesisDevelopmentState]]:
    """Return the real pipe for every declared transition, by phase name."""
    return {
        (item.spec.phase, target): pipe
        for item in phase_declarations()
        for target, pipe in item.edges.items()
    }


def build_hypothesis_development_spec() -> WorkflowSpec[
    DevelopmentEngine,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
]:
    """Assemble and validate the hypothesis-development workflow spec."""
    return build_hypothesis_development_workflow().spec


def build_hypothesis_development_workflow(
    *,
    chat: ChatService | None = None,
    router: ModelRouter | None = None,
    runner: BacktestRunner | None = None,
    research_runner: ResearchModelRunner | None = None,
    programs: ProgramSnapshots | None = None,
    validator: StrategySandboxValidator | None = None,
    hypothesis_store: HypothesisStore | None = None,
    ledger: AttemptLedger | None = None,
    subagent_factory: SubagentContextFactory | None = None,
    journal: CommandJournal | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[str], str] = _new_id,
    research_role: Role = "research",
    experiment_id: str | None = None,
    knowledge_cutoff: datetime | None = None,
    subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    conversations: Conversations | None = None,
    execution_store: ExecutionStore | None = None,
    detach: Detach | None = None,
    knowledge_scope: Literal["shared", "workspace", "both"] = "both",
) -> Workflow[
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDevelopmentOutcome,
]:
    """Build the hypothesis-development workflow as the general object.

    The same construction path serves the registered specification (no ports)
    and the production runtime (all ports, services, and doctrine bound).
    """
    del subagent_factory, knowledge_cutoff
    from .doctrine import hypothesis_development_doctrine
    from .phases import workflow_definition

    declarations = phase_declarations()
    definition = workflow_definition()
    resolved_research_runner = research_runner
    if resolved_research_runner is None and runner is not None:
        resolved_research_runner = BacktestResearchModelRunner(runner)
    if resolved_research_runner is not None and not callable(
        getattr(resolved_research_runner, "run", None)
    ):
        raise WorkflowError("research_runner must implement ResearchModelRunner")
    tools: dict[str, tuple[str, ...]] = {
        family: tuple(family_tools)
        for family, family_tools in dict(subagent_mcp_tools or {}).items()
    }
    services = HypothesisDevelopmentServices(
        store=(
            InMemoryHypothesisStore() if hypothesis_store is None else hypothesis_store
        ),
        router=router,
        runner=runner,
        research_runner=resolved_research_runner,
        programs=programs,
        validator=validator,
        ledger=ledger,
        journal=journal,
        research_role=research_role,
        experiment_id=experiment_id,
        mcp_tools=tools,
        phase_mcp_tools={item.spec.phase: item.mcp_tools for item in declarations},
        phase_tools={item.spec.phase: item.tools for item in declarations},
    )
    builder = WorkflowBuilder[
        DevelopmentEngine,
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
    ]()
    builder.workflow(
        definition.workflow_id,
        version=definition.version,
        title=definition.title,
        knowledge_scope=knowledge_scope,
    )
    for item in declarations:
        if item.knowledge is not None:
            builder.knowledge(item.spec.phase, item.knowledge)
        builder.phase(item.spec)
    builder.transitions(
        {item.spec.phase: frozenset(item.edges) for item in declarations}
    )
    for (source, target), pipe in piping().items():
        builder.piping(source, target, pipe)
    builder.on_start(_start_handoff)
    builder.doctrine(hypothesis_development_doctrine(services))
    builder.services(services)
    if chat is not None:
        builder.ports(
            WorkflowPorts(
                chat=chat,
                clock=clock,
                id_factory=id_factory,
                conversations=conversations,
                execution_store=execution_store,
                detach=detach,
            )
        )
    return cast(Any, builder.build())
