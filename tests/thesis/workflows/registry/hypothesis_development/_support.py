from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
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
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
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
from thesis.execution.events import ExecutionEventType
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence import CommandJournal
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion
from thesis.providers.invocations import Role as ProviderRole
from thesis.research.hypothesis import (
    InMemoryHypothesisStore,
)
from thesis.strategies.validation import (
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.core.execution_store import InMemoryExecutionStore
from thesis.workflows.core.workflow import Detach, Workflow
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentOutcome,
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


def _inline_model() -> dict[str, object]:
    return {
        "modelId": "momentum-model",
        "version": "1.0.0",
        "description": "Estimate the historical momentum continuation regularity.",
        "source": (
            "use harness_strategy_sdk::{\n"
            "    export_study_program, BarFrame, MarketConsumer, ObservationContext,\n"
            "    Subscription, MetricPublisher, StudyProgram,\n"
            "};\n"
            "use harness_strategy_sdk::serde_json::Value;\n"
            "\n"
            "struct MomentumModel;\n"
            "\n"
            "impl MarketConsumer for MomentumModel {\n"
            "    type Context = ObservationContext;\n"
            "    fn subscriptions(&self) -> Vec<Subscription> {\n"
            '        vec![Subscription::Bars { symbols: vec!["SPY".into()],\n'
            '            interval: "1min".into() }]\n'
            "    }\n"
            "    fn on_bar(&mut self, _frame: &BarFrame, _c: &mut ObservationContext) {}\n"
            "}\n"
            "\n"
            "impl StudyProgram for MomentumModel {\n"
            "    fn new(_parameters: Value) -> Self {\n"
            "        Self\n"
            "    }\n"
            "\n"
            "    fn on_stop(&mut self, metrics: &mut MetricPublisher) {\n"
            '        let _ = metrics.publish("sharpe", 1.0);\n'
            '        let _ = metrics.publish("fit.baseline", 0.5);\n'
            "    }\n"
            "}\n"
            "\n"
            "export_study_program!(MomentumModel);\n"
        ),
        "outputs": ["sharpe"],
        "tests": {"test_model.txt": "Covered by the harness jit suite.\n"},
        "harnessConfiguration": {
            "seed": 7,
            "symbols": ["SPY"],
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-03-01T00:00:00Z",
            "subscriptions": [
                {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
            ],
        },
    }


def _model() -> dict[str, object]:
    inline = _inline_model()
    return {
        "workspaceId": WORKSPACE,
        "kind": "study",
        "programId": "momentum-model",
        "gitCommit": "a" * 40,
        "version": "b" * 64,
        "description": inline["description"],
        "outputs": inline["outputs"],
        "tests": inline["tests"],
    }


class FakePrograms:
    """Committed snapshot fixture for reference-based workflow tests."""

    def load(self, workspace_id, kind, program_id, commit=None):
        inline = _inline_model()
        return {
            "workspaceId": workspace_id,
            "kind": kind,
            "programId": program_id,
            "gitCommit": commit or "c" * 40,
            "version": "b" * 64,
            "source": inline["source"],
            "configuration": inline["harnessConfiguration"],
            "sourceSha256": "d" * 64,
            "bundleSha256": "b" * 64,
            "files": {"program.rs": "d" * 64},
            "sdkIdentity": "sdk-current",
            "validationIdentity": "compiler-current",
            "validation": {
                "gitCommit": "a" * 40,
                "workspaceId": workspace_id,
                "kind": kind,
                "programId": program_id,
                "version": "b" * 64,
                "accepted": True,
                "sdkIdentity": "sdk-current",
                "validationIdentity": "compiler-current",
                "sourceSha256": "d" * 64,
                "bundleSha256": "b" * 64,
                "files": {"program.rs": "d" * 64},
            },
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


def _discovery_envelope() -> dict[str, object]:
    return {
        "candidate": {
            "opportunity": (
                "Five-day momentum in SPY continues over the next five days."
            ),
            "signal": "Five-day return sign, measured at each daily close.",
            "informationTime": "At the daily close that ends the five-day window.",
            "direction": "long continuation",
            "holdingPeriod": "five trading days",
            "profitSource": (
                "Slow information diffusion: the other side hedges too early."
            ),
            "effect": "positive conditional mean forward return",
            "competingExplanation": (
                "Time-series momentum proxies a slow macro drift."
            ),
            "evidence": "Small studies showed continuation after five-day gains.",
        },
        "attempts": [
            {
                "purpose": "Check momentum continuation on SPY daily bars.",
                "studyId": "study-momentum",
                "dataTypes": ["bars"],
                "window": "2024-01-01T00:00:00Z to 2024-03-01T00:00:00Z",
                "result": "Positive continuation, weak but consistent.",
                "changed": True,
                "runId": "run-momentum",
            }
        ],
        "untouchedData": "Quote and trade data were left untouched for validation.",
        "limitations": "One market and one regime; the effect is weak.",
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
        "harnessConfiguration": {
            "seed": 7,
            "symbols": ["SPY"],
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}],
        },
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
        if not self._payloads:
            raise AssertionError("model invoked more times than expected")
        payload = self._payloads.pop(0)
        output = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(output=output)


class FakeNativeFixture:
    """Enable captured workers without allowing fixture-owned model execution."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: ProviderRole = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        self.calls += 1
        raise AssertionError("fixture workers must remain captured, not call a model")


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
        native_provider: FakeNativeFixture,
        runner: FakeRunner,
        store: InMemoryHypothesisStore,
        ledger: AttemptLedger,
        factory: RecordingSubagentFactory,
        journal: CommandJournal,
        conversations: Conversations,
        execution_store: InMemoryExecutionStore,
        woke: list[object],
    ) -> None:
        self.workflow = workflow
        self.chat = chat
        self.provider = provider
        self.native_provider = native_provider
        self.runner = runner
        self.store = store
        self.ledger = ledger
        self.factory = factory
        self.journal = journal
        self.conversations = conversations
        self.execution_store = execution_store
        self.woke = woke


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
    detach: Detach | None = None,
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
    sub_agent_store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(
        chat,
        store=sub_agent_store,
        max_depth=max_depth,
        max_children=max_children,
    )
    woke: list[object] = []
    native_provider = FakeNativeFixture()
    conversations = Conversations(
        chat=chat,
        store=sub_agent_store,
        lifecycle=lifecycle,
        provider=native_provider,
        # A dropped wake leaves the conversation in flight, so a test can
        # assert exactly which conversations the engine woke.
        spawn_thread=lambda target: woke.append(target),
    )
    execution_store = InMemoryExecutionStore()
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
        programs=FakePrograms(),
        conversations=conversations,
        execution_store=execution_store,
        detach=detach,
    )
    return _Kit(
        workflow=workflow,
        chat=chat,
        provider=provider,
        native_provider=native_provider,
        runner=runner,
        store=store,
        ledger=ledger,
        factory=factory,
        journal=journal,
        conversations=conversations,
        execution_store=execution_store,
        woke=woke,
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


def _discovery_node_id(kit: _Kit, request: HypothesisDevelopmentRequest) -> str:
    return _phase_node_id(kit, request, "discovery")


def _node_record(kit: _Kit, sub_agent_id: str) -> SubAgentRecord:
    record = PersistenceSubAgentStore(kit.chat.persistence).get_sub_agent(
        WORKSPACE, sub_agent_id
    )
    assert record is not None, f"no durable sub-agent record for {sub_agent_id}"
    return record


def _prompt_with(kit: _Kit, marker: str) -> str:
    for prompt in kit.provider.prompts:
        if marker in prompt:
            return prompt
    raise AssertionError(f"no prompt contains {marker!r}")


def _skill_keys(policy: RolePolicy) -> set[str]:
    return {skill if isinstance(skill, str) else skill.key for skill in policy.skills}


def _events_by_type(
    kit: _Kit, execution_id: str, event_type: ExecutionEventType
) -> list[Any]:
    events = kit.workflow.observe(WORKSPACE, execution_id).events
    return [event for event in events if event.event_type is event_type]
