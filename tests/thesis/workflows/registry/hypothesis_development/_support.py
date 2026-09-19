from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any, cast

from thesis.agent.context import PromptContext
from thesis.agent.router import (
    ModelProfile,
    ModelResponse,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.runtime.delivery import NodeInputService, TurnScheduler
from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
)
from thesis.agent.runtime.policy import (
    AnsweringActor,
    OperatorActorKind,
    OperatorPolicyRegistry,
)
from thesis.agent.runtime.specs import (
    DEFAULT_MAX_CHILDREN,
    DEFAULT_MAX_DEPTH,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.skills import SkillRegistry
from thesis.agent.subagents import SubagentContext, SubagentContextFactory
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    SubAgentRecord,
)
from thesis.chat.service import ChatService
from thesis.chat.workflow_launcher import RuntimeWorkflowLauncher
from thesis.execution.events import ExecutionEventType
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence import CommandJournal
from thesis.research.hypothesis import (
    InMemoryHypothesisStore,
)
from thesis.strategies.validation import (
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows import WorkflowRuntime, build_workflow_runtime
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunRequest,
    BacktestRunResult,
    BacktestRunTimeout,
)
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentPhase,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    build_hypothesis_development_workflow,
)
from thesis.workflows.research_models import ResearchModelRunner

WORKSPACE = "workspace-demo"

OWNER = "owner-1"

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

IDEAS = (
    "Momentum in SPY might keep going because the market underreacts to slow "
    "information diffusion."
)


def _clock() -> Callable[[], datetime]:
    ticks = count(1)

    def clock() -> datetime:
        return NOW.replace(microsecond=next(ticks))

    return clock


def _id_factory() -> Callable[[str], str]:
    counter = count(1)
    return lambda prefix: f"{prefix}-{next(counter)}"


def _model() -> dict[str, object]:
    return {
        "modelId": "momentum-model",
        "version": "1.0.0",
        "description": "Estimate the historical momentum continuation regularity.",
        "source": (
            "def fit(context):\n"
            "    return {'baseline': 0.5}\n\n"
            "def observe(fit, context):\n"
            "    return {'sharpe': 1.0}\n"
        ),
        "fitEntrypoint": "fit",
        "observeEntrypoint": "observe",
        "outputs": ["sharpe"],
        "indicators": [],
        "tests": {"test_model.py": "def test_model():\n    assert True\n"},
        "harnessConfiguration": {"seed": 7, "symbols": ["SPY"]},
    }


def _hypothesis_envelope(
    falsifiers: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "hypothesisId": "momentum-breakout",
        "claim": "Five-day momentum in SPY continues over the next five days.",
        "disagreement": (
            "The market treats momentum as noise; the continuation is "
            "underreaction to slow information diffusion."
        ),
        "riskFactor": "Crowded positioning can compress the edge.",
        "horizon": "5d",
        "universe": ["SPY"],
        "falsifiers": [
            {
                "falsifierId": "f1",
                "observable": "sharpe",
                "condition": "lt",
                "threshold": 0.0,
                "test": "Run the momentum probe and require a negative Sharpe.",
            }
        ]
        if falsifiers is None
        else falsifiers,
        "sizing": "fixed fraction 0.1 of equity",
        "body": "The continuation thesis.",
        "model": _model(),
    }


def _plan_envelope(
    controls: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "tests": [
            {
                "testId": "t1",
                "falsifierId": "f1",
                "description": "Backtest the probe and read the Sharpe.",
            }
        ],
        "controls": [
            {
                "controlId": "c1",
                "description": "Run the same probe on shuffled labels.",
                "expectation": "The edge should vanish.",
            }
        ]
        if controls is None
        else controls,
    }


def _design_envelope(
    exit_criteria: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "strategyId": "momentum-live",
        "version": "1.0.0",
        "files": {"strategy.py": "from harness.strategy import Strategy\n"},
        "tests": {"test_strategy.py": "def test_strategy():\n    assert True\n"},
        "harnessConfiguration": {"seed": 7, "symbols": ["SPY"]},
        "exitCriteria": [
            {
                "falsifierId": "f1",
                "observable": "sharpe",
                "condition": "lt",
                "threshold": 0.0,
                "description": "Exit when the falsifier re-check fires.",
            }
        ]
        if exit_criteria is None
        else exit_criteria,
        "limitations": ["in-sample probe only"],
    }


class FakeProvider:
    """Deterministic fake provider returning queued JSON envelopes."""

    def __init__(self, *payloads: dict[str, object] | str) -> None:
        self._payloads = list(payloads)
        self.prompts: list[str] = []
        self.response_formats: list[object] = []

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
        response_format: object = None,
    ) -> ModelResponse:
        del profile, allowed_tools
        self.prompts.append(prompt.rendered)
        self.response_formats.append(response_format)
        if "operator-facing voice" in prompt.rendered:
            marker = "Concrete reason the workflow needs direction: "
            detail = (
                prompt.rendered.split(marker, 1)[-1].splitlines()[0]
                if marker in prompt.rendered
                else "the workflow needs direction"
            )
            return ModelResponse(output=f"To continue, I need your direction: {detail}")
        if "workflow's verdict gate" in prompt.rendered:
            marker = "Operator's latest message: "
            body = (
                prompt.rendered.split(marker, 1)[-1].splitlines()[0].lower()
                if marker in prompt.rendered
                else ""
            )
            circle = any(
                word in body
                for word in (
                    "circle",
                    "revise",
                    "revisit",
                    "disagree",
                    "not ready",
                    "go back",
                )
            )
            action = (
                "circle_back"
                if circle
                else ("check_off" if body.strip() else "ambiguous")
            )
            return ModelResponse(output=json.dumps({"action": action, "message": "ok"}))
        if "falsification trials orchestrator" in prompt.rendered:
            return ModelResponse(
                output="### Empirical Falsification Findings\n\nAll planned trials survived historical testing."
            )
        if not self._payloads:
            raise AssertionError("model invoked more times than expected")
        payload = self._payloads.pop(0)
        output = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(output=output)


class FakeRunner:
    """Deterministic fake runner returning queued ``(state, metrics)`` pairs."""

    def __init__(
        self, results: list[tuple[str, object | None] | BacktestRunError]
    ) -> None:
        self._results = list(results)
        self.requests: list[BacktestRunRequest] = []

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        result = self._results.pop(0)
        if isinstance(result, BacktestRunError):
            raise result
        state, metrics = result
        if isinstance(metrics, Mapping):
            metrics = {"fit.baseline": 0.5, **dict(metrics)}
        return BacktestRunResult(
            job_id=f"job-{len(self.requests)}",
            state=state,
            mode="local",
            manifest={"manifestId": "manifest-1", "sha256": "c" * 64},
            metrics=metrics,
            result_artifacts=[
                {"uri": "fills.csv", "sha256": "d" * 64, "mediaType": "text/csv"}
            ],
        )


class FakeValidator:
    """Always-accepting sandbox validator behind the published port."""

    def __init__(self) -> None:
        self.requests: list[StrategyValidationRequest] = []

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        self.requests.append(request)
        return StrategyValidationResult(True, (), validation_id="validation-1")


class RecordingSubagentFactory:
    """Subagent factory wrapper that records every fresh context it creates."""

    def __init__(self) -> None:
        self._inner = SubagentContextFactory()
        self.created: list[SubagentContext] = []

    def create(self, **kwargs: Any) -> SubagentContext:
        context = self._inner.create(**kwargs)
        self.created.append(context)
        return context


class _Kit:
    def __init__(
        self,
        *,
        workflow: Workflow[
            HypothesisDevelopmentRequest,
            HypothesisDevelopmentState,
            HypothesisDevelopmentOutcome,
        ],
        chat: ChatService,
        provider: FakeProvider,
        runner: FakeRunner,
        store: InMemoryHypothesisStore,
        ledger: AttemptLedger,
        factory: RecordingSubagentFactory,
        journal: CommandJournal,
    ) -> None:
        self.workflow = workflow
        self.chat = chat
        self.provider = provider
        self.runner = runner
        self.store = store
        self.ledger = ledger
        self.factory = factory
        self.journal = journal


def _kit(
    tmp_path: Path,
    *,
    payloads: list[dict[str, object] | str],
    results: list[tuple[str, object | None] | BacktestRunError],
    exit_criteria: list[dict[str, object]] | None = None,
    prepend_ready: bool = True,
    insert_strategy_success: bool = True,
    research_runner: object | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_children: int = DEFAULT_MAX_CHILDREN,
    turn_scheduler: TurnScheduler | None = None,
) -> _Kit:
    clock = _clock()
    id_factory = _id_factory()
    chat = ChatService(clock=clock, id_factory=id_factory)
    # Most tests model a ready first proposal followed by accepted confirmation.
    queued: list[dict[str, object] | str] = list(payloads)
    if prepend_ready:
        queued = [
            {"message": "Ready for operator review.", "advance": True},
            {"message": "Confirmation accepted.", "advance": True},
            *queued,
        ]
    provider = FakeProvider(*queued)
    profiles = {
        "research-profile": ModelProfile(
            profile_id="research-profile",
            version="1.0.0",
            provider="fake",
            model="research-model",
        ),
        "critique-profile": ModelProfile(
            profile_id="critique-profile",
            version="1.0.0",
            provider="fake",
            model="critique-model",
        ),
        "coding-profile": ModelProfile(
            profile_id="coding-profile",
            version="1.0.0",
            provider="fake",
            model="coding-model",
        ),
    }
    policies: dict[Role, RolePolicy] = {
        "research": RolePolicy(
            role="research", version="1.0.0", model_profile="research-profile"
        ),
        "critique": RolePolicy(
            role="critique", version="1.0.0", model_profile="critique-profile"
        ),
        "coding": RolePolicy(
            role="coding", version="1.0.0", model_profile="coding-profile"
        ),
    }
    router = ModelRouter(
        role_policies=policies,
        model_profiles=profiles,
        providers={"fake": provider},
        skills=SkillRegistry(),
        bundle=PolicyBundle(
            bundle_id="thesis-platform",
            version="1.0.0",
            bundle_sha256="a" * 64,
            policy_sha256="b" * 64,
        ),
        clock=clock,
        id_factory=lambda: id_factory("invocation"),
    )
    store = InMemoryHypothesisStore()
    ledger = AttemptLedger()
    factory = RecordingSubagentFactory()
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    runner_results = list(results)
    if insert_strategy_success and len(runner_results) >= 2:
        runner_results.insert(1, ("completed", {"sharpe": 1.0}))
    runner = FakeRunner(runner_results)
    workflow = build_hypothesis_development_workflow(
        chat=chat,
        router=router,
        runner=runner,
        validator=FakeValidator(),
        hypothesis_store=store,
        ledger=ledger,
        subagent_factory=cast("SubagentContextFactory", factory),
        journal=journal,
        clock=clock,
        id_factory=id_factory,
        experiment_id="experiment-1",
        research_runner=(
            cast("ResearchModelRunner", research_runner)
            if research_runner is not None
            else None
        ),
        subagent_lifecycle=SubAgentLifecycle(
            chat,
            store=PersistenceSubAgentStore(chat.persistence),
            max_depth=max_depth,
            max_children=max_children,
        ),
        turn_scheduler=turn_scheduler,
    )
    return _Kit(
        workflow=workflow,
        chat=chat,
        provider=provider,
        runner=runner,
        store=store,
        ledger=ledger,
        factory=factory,
        journal=journal,
    )


def _request(kit: _Kit, ideas: str = IDEAS):
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="hypothesis chat", owner_actor_id=OWNER
    ).chat_id
    trigger = kit.chat.create_message(WORKSPACE, chat_id, actor_id=OWNER, body=ideas)
    return kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        ideas=ideas,
        trigger_message_id=trigger.message_id,
    )


def _phase_node_id(kit: _Kit, request: HypothesisDevelopmentRequest, phase: str) -> str:
    for event in reversed(kit.workflow.observe(WORKSPACE, request.execution_id).events):
        if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
            continue
        if event.payload.get("phase") == phase:
            return str(event.payload["subAgentId"])
    raise AssertionError(f"no {phase} node was spawned")


def _workflow_node(kit: _Kit, request: HypothesisDevelopmentRequest) -> SubAgentRecord:
    for record in PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(
        WORKSPACE
    ):
        if (
            record.workflow_execution_id == request.execution_id
            and record.role == WORKFLOW_NODE_ROLE
        ):
            return record
    raise AssertionError("no durable workflow node was created")


def _creation_node_id(kit: _Kit, request: HypothesisDevelopmentRequest) -> str:
    return _phase_node_id(kit, request, "hypothesis_creation")


def _operator_questions(kit: _Kit, request: HypothesisDevelopmentRequest) -> list[Any]:
    """Return the operator questions delivered on the unified delivery path."""
    return [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.execution_id == request.execution_id
        and message.data is not None
        and message.data.get("kind") == "operator_question"
    ]


def _question_node_id(kit: _Kit, request: HypothesisDevelopmentRequest) -> str:
    questions = _operator_questions(kit, request)
    assert questions, "no operator question was delivered"
    return str(questions[-1].data["subAgentId"])


def _node_record(kit: _Kit, sub_agent_id: str) -> SubAgentRecord:
    record = PersistenceSubAgentStore(kit.chat.persistence).get_sub_agent(
        WORKSPACE, sub_agent_id
    )
    assert record is not None, f"no durable sub-agent record for {sub_agent_id}"
    return record


def _latest_input_phase(kit: _Kit, request: HypothesisDevelopmentRequest) -> str:
    questions = _operator_questions(kit, request)
    assert questions, "no operator question was delivered"
    return str(questions[-1].data["phase"])


def _answer_current_pause(
    kit: _Kit, request: HypothesisDevelopmentRequest, *, body: str | None = None
) -> HypothesisDevelopmentOutcome:
    """Answer the latest operator request through the unified node-input path.

    The answering actor is the authenticated human operator; the node's
    operator policy adjudicates the answer, and the scheduled node turn is
    exactly one continuation of the same execution, which is never paused.
    """
    phase = _latest_input_phase(kit, request)
    resolved = body or ("check off" if phase == "verdict" else "confirm")
    outcomes: list[HypothesisDevelopmentOutcome] = []
    service = NodeInputService(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        policies=OperatorPolicyRegistry(),
        schedule=lambda _ws, _chat_id, _node: outcomes.append(
            kit.workflow.run(request)
        ),
    )
    receipt = service.accept(
        WORKSPACE,
        request.chat_id,
        _question_node_id(kit, request),
        body=resolved,
        actor=AnsweringActor(actor_id=OWNER, kind=OperatorActorKind.HUMAN_OPERATOR),
    )
    assert receipt.duplicate is False
    assert receipt.scheduled is True
    assert receipt.decision.actor.kind is OperatorActorKind.HUMAN_OPERATOR
    assert len(outcomes) == 1
    return outcomes[-1]


def _prompt_with(kit: _Kit, marker: str) -> str:
    for prompt in kit.provider.prompts:
        if marker in prompt:
            return prompt
    raise AssertionError(f"no prompt contains {marker!r}")


def _run(kit: _Kit, request: HypothesisDevelopmentRequest, confirm: str = "confirm"):
    """Drive the conversational phases as far as the queued model turns allow.

    The execution is never paused: each delivered operator request is answered
    through the unified node-input path, and the scheduled continuation
    re-enters ``run``.  A phase that needs more operator input simply returns,
    so a gate failure is observable as its own awaiting outcome.
    """
    outcome = kit.workflow.run(request)
    if (
        outcome.phase is HypothesisDevelopmentPhase.HYPOTHESIS_CREATION
        and _operator_questions(kit, request)
    ):
        outcome = _answer_current_pause(kit, request, body=confirm)
    if outcome.phase is HypothesisDevelopmentPhase.VERDICT:
        outcome = _answer_current_pause(kit, request)
    return outcome


def _skill_keys(policy: RolePolicy) -> set[str]:
    return {skill if isinstance(skill, str) else skill.key for skill in policy.skills}


def _events_by_type(
    kit: _Kit, execution_id: str, event_type: ExecutionEventType
) -> list[Any]:
    events = kit.workflow.observe(WORKSPACE, execution_id).events
    return [event for event in events if event.event_type is event_type]


class _StubInvocation:
    """Minimal invocation stand-in exposing only the composer input surface."""

    def __init__(self, output: str) -> None:
        self.response = ModelResponse(output=output)


def _fenced(envelope: dict[str, object]) -> str:
    return f"```json\n{json.dumps(envelope)}\n```"


class _TimingOutResearchRunner:
    """A research-model port whose run has not reached a terminal state."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def run(self, request: object) -> object:
        self.requests.append(request)
        raise BacktestRunTimeout(
            "study 'falsify-execution-1-model' did not reach a terminal state "
            "within 3900s"
        )


def _runtime_kit(
    chat: ChatService,
    *,
    payloads: list[dict[str, object] | str],
    results: list[tuple[str, object | None] | BacktestRunError],
    turn_scheduler: TurnScheduler | None = None,
) -> tuple[WorkflowRuntime, FakeProvider]:
    """Compose the production runtime over the same deterministic fakes."""
    clock = _clock()
    id_factory = _id_factory()
    provider = FakeProvider(*payloads)
    profiles = {
        "research-profile": ModelProfile(
            profile_id="research-profile",
            version="1.0.0",
            provider="fake",
            model="research-model",
        ),
        "critique-profile": ModelProfile(
            profile_id="critique-profile",
            version="1.0.0",
            provider="fake",
            model="critique-model",
        ),
        "coding-profile": ModelProfile(
            profile_id="coding-profile",
            version="1.0.0",
            provider="fake",
            model="coding-model",
        ),
    }
    policies: dict[Role, RolePolicy] = {
        "research": RolePolicy(
            role="research", version="1.0.0", model_profile="research-profile"
        ),
        "critique": RolePolicy(
            role="critique", version="1.0.0", model_profile="critique-profile"
        ),
        "coding": RolePolicy(
            role="coding", version="1.0.0", model_profile="coding-profile"
        ),
    }
    runtime = build_workflow_runtime(
        role_policies=policies,
        model_profiles=profiles,
        providers={"fake": provider},
        skills=SkillRegistry(),
        bundle=PolicyBundle(
            bundle_id="thesis-platform",
            version="1.0.0",
            bundle_sha256="a" * 64,
            policy_sha256="b" * 64,
        ),
        runner=FakeRunner(results),
        strategy_validator=FakeValidator(),
        hypothesis_store=InMemoryHypothesisStore(),
        subagent_lifecycle=SubAgentLifecycle(
            chat, store=PersistenceSubAgentStore(chat.persistence)
        ),
        turn_scheduler=turn_scheduler,
        clock=clock,
        id_factory=id_factory,
    )
    return runtime, provider


def _chat_for_runtime() -> ChatService:
    return ChatService(clock=_clock(), id_factory=_id_factory())


def _launch_runtime_workflow(
    runtime: WorkflowRuntime,
    chat: ChatService,
    *,
    turn_scheduler: TurnScheduler | None = None,
) -> tuple[str, str]:
    """Launch through the runtime launcher and return ``(chat_id, execution_id)``."""
    chat_id = chat.create_chat(
        WORKSPACE, title="runtime chat", owner_actor_id=OWNER
    ).chat_id
    trigger = chat.create_message(WORKSPACE, chat_id, actor_id=OWNER, body=IDEAS)
    launcher = RuntimeWorkflowLauncher(
        runtime,
        chat,
        detach=lambda function: function(),
        turn_scheduler=turn_scheduler,
    )
    handle = launcher.start_hypothesis_development(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        actor_id=OWNER,
        ideas=IDEAS,
        trigger_message_id=trigger.message_id,
    )
    return chat_id, str(handle["executionId"])


def _execution_questions(
    chat: ChatService, chat_id: str, execution_id: str
) -> list[Any]:
    return [
        message
        for message in chat.list_messages(WORKSPACE, chat_id)
        if message.execution_id == execution_id
        and message.data is not None
        and message.data.get("kind") == "operator_question"
    ]


def _answer_through_scheduler(
    runtime: WorkflowRuntime,
    chat: ChatService,
    scheduled: list[tuple[str, str, str]],
    *,
    chat_id: str,
    execution_id: str,
    node_id: str,
    body: str,
) -> HypothesisDevelopmentOutcome:
    """Accept an operator answer and dispatch its turn through the runtime."""
    outcomes: list[HypothesisDevelopmentOutcome] = []

    def dispatch(workspace_id: str, target_chat_id: str, sub_agent_id: str) -> None:
        scheduled.append((workspace_id, target_chat_id, sub_agent_id))
        request = HypothesisDevelopmentRequest.from_execution(
            chat, chat.get_execution(workspace_id, execution_id)
        )
        outcomes.append(runtime.execute_hypothesis_development(request, chat=chat))

    NodeInputService(
        chat=chat,
        store=PersistenceSubAgentStore(chat.persistence),
        policies=OperatorPolicyRegistry(),
        schedule=dispatch,
    ).accept(
        WORKSPACE,
        chat_id,
        node_id,
        body=body,
        actor=AnsweringActor(actor_id=OWNER, kind=OperatorActorKind.HUMAN_OPERATOR),
    )
    assert len(outcomes) == 1
    return outcomes[-1]
