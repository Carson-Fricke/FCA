from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest

from thesis.agent.context import PromptContext
from thesis.agent.hypothesis_skills import (
    FALSIFICATION_RCA_SKILL_ID,
    HYPOTHESIS_AUTHORING_SKILL_ID,
    HYPOTHESIS_DEVELOPMENT_SKILLS,
    STRATEGY_AUTHORING_SKILL_ID,
    register_hypothesis_development_skills,
    with_hypothesis_development_skills,
)
from thesis.agent.router import (
    InvocationResult,
    ModelProfile,
    ModelResponse,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.runtime.lifecycle import (
    SubAgentLifecycle,
    SubAgentLifecycleError,
)
from thesis.agent.runtime.specs import (
    DEFAULT_MAX_CHILDREN,
    DEFAULT_MAX_DEPTH,
    SubAgentSpec,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.skills import SkillRegistry
from thesis.agent.subagents import SubagentContext, SubagentContextFactory
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    MessageRole,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    TranscriptItemKind,
    WorkflowExecutionState,
)
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.execution.sub_agents import SubAgentTree
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence import CommandJournal
from thesis.research.hypothesis import (
    HypothesisStatus,
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
    BacktestRunTimeout,
)
from thesis.workflows.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_PHASES,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    FalsificationComposeError,
    HypothesisDevelopmentIntakeError,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentPhase,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentWorkflow,
    InvalidPhaseTransition,
    _json_document,
    _metric,
    check_evidence_bound,
    check_falsifiability,
    check_falsification_plan,
    check_strategy_design,
    check_verdict_advances,
    transition_phase,
)

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
            marker = "Concrete reason the workflow paused: "
            detail = (
                prompt.rendered.split(marker, 1)[-1].splitlines()[0]
                if marker in prompt.rendered
                else "the workflow paused"
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
        workflow: HypothesisDevelopmentWorkflow,
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
    workflow = HypothesisDevelopmentWorkflow(
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
        research_runner=research_runner,
        subagent_lifecycle=SubAgentLifecycle(
            chat,
            store=PersistenceSubAgentStore(chat.persistence),
            max_depth=max_depth,
            max_children=max_children,
        ),
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


def _latest_input_phase(kit: _Kit, request: HypothesisDevelopmentRequest) -> str:
    needs_input = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    )
    return str(needs_input[-1].payload["phase"])


def _answer_current_pause(
    kit: _Kit, request: HypothesisDevelopmentRequest, *, body: str | None = None
) -> HypothesisDevelopmentOutcome:
    phase = _latest_input_phase(kit, request)
    if body is None:
        body = "check off" if phase == "verdict" else "confirm"
    node_id = _phase_node_id(kit, request, phase)
    kit.chat.create_message(
        WORKSPACE,
        request.chat_id,
        actor_id=OWNER,
        body=body,
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        execution_id=request.execution_id,
        data={
            "executionId": request.execution_id,
            "parentSubAgentId": None,
            "phase": phase,
            "subAgentId": node_id,
        },
    )
    return kit.workflow.resume(request)


def _prompt_with(kit: _Kit, marker: str) -> str:
    for prompt in kit.provider.prompts:
        if marker in prompt:
            return prompt
    raise AssertionError(f"no prompt contains {marker!r}")


def _run(kit: _Kit, request: HypothesisDevelopmentRequest, confirm: str = "confirm"):
    """Drive the conversational phase to completion.

    ``run`` opens the hypothesis-creation conversation and pauses for the
    operator; this helper posts the confirming message and resumes, which
    finalizes the hypothesis and runs the remaining phases.
    """
    outcome = kit.workflow.run(request)
    if outcome.phase is not HypothesisDevelopmentPhase.PAUSED:
        return outcome
    if _latest_input_phase(kit, request) == "hypothesis_creation":
        outcome = _answer_current_pause(kit, request, body=confirm)
    if (
        outcome.phase is HypothesisDevelopmentPhase.PAUSED
        and _latest_input_phase(kit, request) == "verdict"
    ):
        outcome = _answer_current_pause(kit, request)
    return outcome


def test_hypothesis_development_end_to_end(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(),
        ],
        results=[
            ("completed", {"sharpe": 1.5}),
            ("completed", {"sharpe": 1.2}),
        ],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert outcome.fenced is False
    assert outcome.verdict is not None
    assert outcome.verdict.outcome == VERDICT_SUPPORTED
    assert outcome.strategy_bundle is not None
    assert outcome.optimization_backtest is not None
    assert outcome.journal_id == f"workflow-{request.execution_id}"
    assert [item.to_phase for item in outcome.transitions] == [
        HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
        HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
        HypothesisDevelopmentPhase.VERDICT,
        HypothesisDevelopmentPhase.STRATEGY_DESIGN,
        HypothesisDevelopmentPhase.STRATEGY_BACKTESTING,
        HypothesisDevelopmentPhase.OPTIMIZATION,
        HypothesisDevelopmentPhase.COMPLETED,
    ]
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    assert execution.phase == "completed"
    assert len(kit.runner.requests) == 3
    # The strategy bundle materializes the universe and the replay window so a
    # cold backtest fetches and replays the declared symbols instead of falling
    # back to the harness's two-day synthetic default.
    model_request = kit.runner.requests[0]
    assert model_request.run_id.startswith(f"falsify-{request.execution_id}-")
    assert model_request.run_id.endswith("-model")
    strategy_request = kit.runner.requests[1]
    assert strategy_request.run_id.startswith(f"strategy-test-{request.execution_id}-")
    strategy_config = cast(
        "Mapping[str, object]", strategy_request.bundle.harness_configuration
    )
    strategy_data = cast("Mapping[str, object]", strategy_config["data"])
    assert list(cast("Sequence[object]", strategy_data["symbols"])) == ["SPY"]
    assert strategy_config["start"] == "2024-01-01T00:00:00Z"
    assert str(strategy_config["end"]).endswith("T00:00:00Z")
    assert kit.runner.requests[2].run_id.startswith(f"optimize-{request.execution_id}-")

    stored = kit.store.load("momentum-breakout")
    assert stored.status is HypothesisStatus.SUPPORTED
    assert stored.front_matter.links.runs == ("job-1",)
    assert stored.front_matter.links.evidence
    assert outcome.research_model_reference is not None
    assert outcome.research_model_reference.workspace_id == WORKSPACE
    assert outcome.research_model_artifact is not None
    assert outcome.research_model_artifact.startswith("models/runs/")
    assert outcome.as_dict()["researchModel"]["evidenceArtifact"] == (
        outcome.research_model_artifact
    )
    records = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    assert records
    assert all(
        record.workflow_execution_id == request.execution_id for record in records
    )
    assert all(record.status.value in {"completed", "failed"} for record in records)
    assert outcome.strategy_bundle is not None
    assert outcome.research_model_reference.key in outcome.strategy_bundle.dependencies
    assert kit.ledger.entries()
    kit.ledger.verify()
    kit.journal.verify_integrity()


def test_first_creation_turn_requires_explicit_confirmation(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.PAUSED
    needs_input = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    )
    question = str(needs_input[-1].payload["question"])
    assert "hypothesis" in question.lower()
    assert needs_input[-1].payload["ready"] is True


def test_structured_finalization_emits_only_concise_summary(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    outputs = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_OUTPUT
    )
    text = [
        str(event.payload.get("artifact", {}).get("content", "")) for event in outputs
    ]
    assert any(item.startswith("Hypothesis finalization recorded.") for item in text)
    assert not any(item.lstrip().startswith("{") for item in text)


def test_falsification_agent_explains_results_before_visual_report(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    outputs = [
        event
        for event in _events_by_type(
            kit, request.execution_id, ExecutionEventType.SUB_AGENT_OUTPUT
        )
        if event.payload.get("phase") == "falsification_trials"
    ]
    artifacts = [cast("dict[str, Any]", event.payload["artifact"]) for event in outputs]
    prose_index = next(
        index
        for index, artifact in enumerate(artifacts)
        if artifact.get("type") == "text"
        and "Empirical Falsification Findings" in str(artifact.get("content"))
    )
    report_index = next(
        index
        for index, artifact in enumerate(artifacts)
        if artifact.get("type") == "falsification_report"
    )
    assert prose_index < report_index
    assert any(
        "falsification trials orchestrator" in prompt for prompt in kit.provider.prompts
    )


def test_launch_accepts_multiline_hypothesis_ideas(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])

    request = _request(kit, f"FLAG.\n\n{IDEAS}")

    assert request.ideas == f"FLAG.\n\n{IDEAS}"
    assert len(kit.chat.list_executions(WORKSPACE)) == 1


def test_invalid_ideas_do_not_persist_an_orphaned_execution(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="hypothesis chat", owner_actor_id=OWNER
    ).chat_id

    with pytest.raises(HypothesisDevelopmentIntakeError, match="unsupported"):
        kit.workflow.launch(
            WORKSPACE,
            chat_id,
            actor_id=OWNER,
            ideas=f"{IDEAS}\x00",
        )

    assert kit.chat.list_executions(WORKSPACE) == ()
    assert kit.chat.list_messages(WORKSPACE, chat_id) == ()


def test_each_phase_emits_a_workflow_phase_event(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    phases = [
        event.payload["phase"]
        for event in replay.events
        if event.event_type is ExecutionEventType.WORKFLOW_PHASE
    ]
    assert phases == [
        "hypothesis_creation",
        "paused",
        "falsification_trials",
        "verdict",
        "paused",
        "strategy_design",
        "strategy_backtesting",
        "optimization",
        "completed",
    ]


def test_falsifiability_gate_pauses_without_advancing(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(falsifiers=[])],
        results=[],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.detail is not None
    assert "falsifier" in outcome.detail
    assert kit.runner.requests == []
    assert outcome.hypothesis is None


def test_model_gate_requires_every_falsifier_observable(tmp_path: Path) -> None:
    envelope = _hypothesis_envelope()
    model = dict(_model())
    model["outputs"] = ["some_other_statistic"]
    envelope["model"] = model
    kit = _kit(tmp_path, payloads=[envelope], results=[])
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.detail is not None
    assert "missing model outputs" in outcome.detail
    assert kit.runner.requests == []


def test_falsification_plan_gate_pauses_without_a_control(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(controls=[])],
        results=[],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.detail is not None
    assert "control" in outcome.detail
    assert kit.runner.requests == []


def test_verdict_review_returns_refuted_premise_to_hypothesis(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            {
                "message": "I revised the premise from the failed trials.",
                "advance": True,
            },
        ],
        results=[("completed", {"sharpe": -0.5})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.verdict is not None
    assert outcome.verdict.outcome == VERDICT_REFUTED
    assert outcome.detail is not None
    assert "review" in outcome.detail.lower()
    assert outcome.strategy_bundle is None
    assert kit.store.load("momentum-breakout").status is HypothesisStatus.REFUTED
    assert (
        _events_by_type(
            kit, request.execution_id, ExecutionEventType.WORKFLOW_GATE_BLOCKED
        )
        == []
    )
    verdict_questions = [
        event
        for event in _events_by_type(
            kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
        )
        if event.payload.get("phase") == "verdict"
    ]
    assert verdict_questions
    assert verdict_questions[-1].payload["acceptsInput"] is True
    handoffs = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.kind is TranscriptItemKind.SUB_AGENT
        and message.role is MessageRole.SYSTEM
    ]
    assert len(handoffs) == 1
    assert "observed=-0.5000" in handoffs[0].body
    hypothesis_nodes = [
        event
        for event in _events_by_type(
            kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
        )
        if event.payload["phase"] == "hypothesis_creation"
    ]
    assert len(hypothesis_nodes) == 1
    hypothesis_needs_input = [
        event
        for event in _events_by_type(
            kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
        )
        if event.payload.get("phase") == "hypothesis_creation"
    ]
    assert len(hypothesis_needs_input) == 2
    assert "attempt 2" in hypothesis_needs_input[-1].payload["question"]
    assert hypothesis_needs_input[-1].payload["acceptsInput"] is True
    hypothesis_outputs = [
        event
        for event in _events_by_type(
            kit, request.execution_id, ExecutionEventType.SUB_AGENT_OUTPUT
        )
        if event.payload.get("phase") == "hypothesis_creation"
    ]
    latest_output = str(hypothesis_outputs[-1].payload["artifact"]["content"])
    assert "attempt 2" in latest_output


def test_strategy_design_gate_requires_falsifier_exit_criteria(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(
                exit_criteria=[
                    {
                        "observable": "total_return",
                        "condition": "lt",
                        "threshold": 0.0,
                        "description": "A profit target is not a falsifier re-check.",
                    }
                ]
            ),
        ],
        results=[("completed", {"sharpe": 1.5})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.detail is not None
    assert "exit criterion" in outcome.detail
    assert outcome.optimization_backtest is None


def test_failed_strategy_backtest_runs_rca_and_reuses_phase_threads(
    tmp_path: Path,
) -> None:
    diagnosis: dict[str, object] = {
        "falsifierId": "strategy_backtest",
        "failureClass": "strategy",
        "rationale": "Entry timing caused excessive adverse selection.",
        "confidence": 0.8,
        "followUps": ["Delay entry until the next bar and widen the exit band."],
    }
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(),
            diagnosis,
            _design_envelope(),
        ],
        results=[
            ("completed", {"sharpe": 1.5}),
            ("failed", None),
            ("completed", {"sharpe": 1.1}),
            ("completed", {"sharpe": 1.2}),
        ],
        insert_strategy_success=False,
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert len(outcome.rca) == 1
    assert outcome.rca[0].failure_class == "strategy"
    assert outcome.strategy_cycles == 2
    assert len(kit.runner.requests) == 4
    phases = [item.to_phase for item in outcome.transitions]
    assert HypothesisDevelopmentPhase.STRATEGY_RCA in phases
    assert phases.count(HypothesisDevelopmentPhase.STRATEGY_DESIGN) == 2
    assert phases.count(HypothesisDevelopmentPhase.STRATEGY_BACKTESTING) == 2
    spawned = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
    )
    assert (
        len([event for event in spawned if event.payload["phase"] == "strategy_design"])
        == 1
    )
    assert (
        len([event for event in spawned if event.payload["phase"] == "strategy_rca"])
        == 1
    )
    handoffs = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.data and message.data.get("handoffKind") == "strategy_rca_findings"
    ]
    assert len(handoffs) == 1
    assert "adverse selection" in handoffs[0].body


def test_research_launch_failures_pause_without_strategy_rca(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
        ],
        results=[BacktestRunError("MCP launch failed")],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.rca == ()
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    jobs = [
        kit.chat.get_tool_job(WORKSPACE, tool_job_id)
        for tool_job_id in execution.tool_job_ids
    ]
    assert len(jobs) == 1
    assert all(job.state.value == "failed" for job in jobs)
    assert len(kit.factory.created) == 0
    assert len(kit.runner.requests) == 1
    phases = [item.to_phase for item in outcome.transitions]
    assert HypothesisDevelopmentPhase.STRATEGY_RCA not in phases


def test_data_validity_failure_pauses_as_execution_problem_without_verdict_refutation(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
        ],
        results=[("completed", {"sample_valid": 0, "sharpe": 0.0})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.verdict is None
    needs_input = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    )
    assert len(needs_input) >= 1
    assert "falsifier" in str(needs_input[-1].payload.get("question", ""))


def test_hypothesis_loop_pauses_after_five_refuted_attempts(tmp_path: Path) -> None:
    payloads: list[dict[str, object] | str] = []
    for cycle in range(1, 6):
        payloads.extend([_hypothesis_envelope(), _plan_envelope()])
        if cycle < 5:
            payloads.extend(
                [
                    {
                        "message": f"Revised hypothesis proposal {cycle + 1}.",
                        "advance": True,
                    },
                    {"message": "Revision confirmed.", "advance": True},
                ]
            )
    kit = _kit(
        tmp_path,
        payloads=payloads,
        results=[("completed", {"sharpe": -0.5}) for _ in range(5)],
        insert_strategy_success=False,
    )
    request = _request(kit)

    outcome = _run(kit, request)
    for _ in range(10):
        if outcome.phase is not HypothesisDevelopmentPhase.PAUSED:
            break
        phase = _latest_input_phase(kit, request)
        body = (
            "The revision addresses the findings; proceed."
            if phase == "hypothesis_creation"
            else "check off"
        )
        outcome = _answer_current_pause(kit, request, body=body)

    assert outcome.phase is HypothesisDevelopmentPhase.FAILED
    assert outcome.hypothesis_cycles == 5
    assert outcome.detail is not None and "5 attempts" in outcome.detail
    spawned = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
    )
    assert (
        len(
            [
                event
                for event in spawned
                if event.payload["phase"] == "hypothesis_creation"
            ]
        )
        == 1
    )


def test_strategy_loop_pauses_after_five_failed_attempts(tmp_path: Path) -> None:
    diagnosis = {
        "falsifierId": "strategy_backtest",
        "failureClass": "strategy",
        "rationale": "The implementation did not preserve the measured edge.",
        "confidence": 0.8,
        "followUps": ["Revise the entry timing and risk controls."],
    }
    payloads: list[dict[str, object] | str] = [
        _hypothesis_envelope(),
        _plan_envelope(),
    ]
    for cycle in range(5):
        payloads.append(_design_envelope())
        payloads.append(
            {**diagnosis, "rationale": f"Strategy attempt {cycle + 1} failed."}
        )
    kit = _kit(
        tmp_path,
        payloads=payloads,
        results=[
            ("completed", {"sharpe": 1.5}),
            *[("failed", None) for _ in range(5)],
        ],
        insert_strategy_success=False,
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.FAILED
    assert outcome.strategy_cycles == 5
    assert len(outcome.rca) == 5
    assert outcome.detail is not None and "5 attempts" in outcome.detail
    spawned = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
    )
    assert (
        len([event for event in spawned if event.payload["phase"] == "strategy_design"])
        == 1
    )
    assert (
        len([event for event in spawned if event.payload["phase"] == "strategy_rca"])
        == 1
    )


def test_failed_optimization_reenters_strategy_rca_and_design(tmp_path: Path) -> None:
    diagnosis: dict[str, object] = {
        "falsifierId": "strategy_backtest",
        "failureClass": "strategy",
        "rationale": "The optimization run exposed unstable exit timing.",
        "confidence": 0.85,
        "followUps": ["Stabilize the exit window before rerunning optimization."],
    }
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(),
            diagnosis,
            _design_envelope(),
        ],
        results=[
            ("completed", {"sharpe": 1.5}),
            ("completed", {"sharpe": 1.1}),
            ("failed", None),
            ("completed", {"sharpe": 1.2}),
            ("completed", {"sharpe": 1.3}),
        ],
        insert_strategy_success=False,
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert outcome.strategy_cycles == 2
    assert len(outcome.rca) == 1
    phases = [transition.to_phase for transition in outcome.transitions]
    assert phases.count(HypothesisDevelopmentPhase.OPTIMIZATION) == 2
    assert phases.count(HypothesisDevelopmentPhase.STRATEGY_RCA) == 1


def test_phase_transition_table_is_explicit_and_auditable() -> None:
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.REQUESTED,
            HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
        )
        is HypothesisDevelopmentPhase.HYPOTHESIS_CREATION
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
            HypothesisDevelopmentPhase.VERDICT,
        )
        is HypothesisDevelopmentPhase.VERDICT
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.STRATEGY_BACKTESTING,
            HypothesisDevelopmentPhase.STRATEGY_RCA,
        )
        is HypothesisDevelopmentPhase.STRATEGY_RCA
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.STRATEGY_RCA,
            HypothesisDevelopmentPhase.STRATEGY_DESIGN,
        )
        is HypothesisDevelopmentPhase.STRATEGY_DESIGN
    )
    with pytest.raises(InvalidPhaseTransition, match="requested to verdict"):
        transition_phase(
            HypothesisDevelopmentPhase.REQUESTED,
            HypothesisDevelopmentPhase.VERDICT,
        )
    with pytest.raises(
        InvalidPhaseTransition, match="completed to hypothesis_creation"
    ):
        transition_phase(
            HypothesisDevelopmentPhase.COMPLETED,
            HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
        )
    assert HYPOTHESIS_DEVELOPMENT_WORKFLOW.phases == HYPOTHESIS_DEVELOPMENT_PHASES


def test_gate_checks_are_individually_testable() -> None:
    from thesis.research.hypothesis import (
        HypothesisFalsifier,
        HypothesisFrontMatter,
        HypothesisStatus,
    )
    from thesis.workflows.hypothesis_development import (
        ExitCriterion,
        FalsificationPlan,
        FalsificationTrial,
        FalsificationVerdict,
        FalsifierTest,
        PlaceboControl,
    )

    falsifier = HypothesisFalsifier(
        observable="sharpe",
        condition="lt",
        threshold=0.0,
        test="run the probe",
        falsifier_id="f1",
    )
    front = HypothesisFrontMatter(
        hypothesis_id="h-1",
        status=HypothesisStatus.ACTIVE,
        claim="claim",
        disagreement="disagreement",
        risk_factor="risk",
        horizon="5d",
        universe=("SPY",),
        falsifiers=(falsifier,),
    )
    assert check_falsifiability(front) is None

    empty = front.model_copy(update={"falsifiers": ()})
    assert check_falsifiability(empty) is not None

    plan = FalsificationPlan(
        tests=(
            FalsifierTest(
                test_id="t1",
                falsifier_id="f1",
                observable="sharpe",
                condition="lt",
                threshold=0.0,
                description="backtest",
            ),
        ),
        controls=(),
    )
    assert check_falsification_plan(plan, front) is not None
    with_control = FalsificationPlan(
        tests=plan.tests,
        controls=(PlaceboControl("c1", "shuffle", "no edge"),),
    )
    assert check_falsification_plan(with_control, front) is None

    trial = FalsificationTrial(
        test_id="t1",
        falsifier_id="f1",
        run_id="run-1",
        digest="a" * 64,
        run_state="completed",
        observed=1.5,
        falsified=False,
    )
    assert check_evidence_bound(with_control, (trial,)) is None
    unbound = trial.__class__(
        test_id="t1",
        falsifier_id="f1",
        run_id="run-1",
        digest="a" * 64,
        run_state="aborted",
        observed=None,
        falsified=False,
    )
    assert check_evidence_bound(with_control, (unbound,)) is not None

    assert (
        check_verdict_advances(FalsificationVerdict(VERDICT_REFUTED, "refuted"))
        is not None
    )
    assert (
        check_verdict_advances(FalsificationVerdict(VERDICT_SUPPORTED, "survived"))
        is None
    )

    from thesis.strategies.bundles import StrategyBundle

    bundle = StrategyBundle.create(
        strategy_id="s",
        version="1.0.0",
        source={"strategy.py": "x = 1\n"},
        tests={"test_s.py": "def test():\n    assert True\n"},
    )
    matching = (ExitCriterion("sharpe", "lt", 0.0, "exit", "f1"),)
    assert check_strategy_design(bundle, matching, front) is None
    mismatching = (ExitCriterion("total_return", "lt", 0.0, "exit"),)
    assert check_strategy_design(bundle, mismatching, front) is not None


def _skill_keys(policy: RolePolicy) -> set[str]:
    return {skill if isinstance(skill, str) else skill.key for skill in policy.skills}


def test_phase_skills_register_and_wire_role_policies() -> None:
    registry = SkillRegistry()
    registered = register_hypothesis_development_skills(registry)
    assert len(registered) == len(HYPOTHESIS_DEVELOPMENT_SKILLS)
    assert registry.resolve(HYPOTHESIS_AUTHORING_SKILL_ID).version == "1.2.0"
    assert registry.resolve(FALSIFICATION_RCA_SKILL_ID).version == "1.2.0"

    research = with_hypothesis_development_skills(
        RolePolicy(role="research", version="1.0.0", model_profile="research-profile")
    )
    assert _skill_keys(research) == {
        "hypothesis-authoring@1.2.0",
        "falsification-planning@1.2.0",
    }
    coding = with_hypothesis_development_skills(
        RolePolicy(role="coding", version="1.0.0", model_profile="coding-profile")
    )
    assert _skill_keys(coding) == {STRATEGY_AUTHORING_SKILL_ID + "@1.2.0"}
    primary = with_hypothesis_development_skills(
        RolePolicy(role="primary", version="1.0.0", model_profile="primary-profile")
    )
    assert primary.skills == ()


def _events_by_type(
    kit: _Kit, execution_id: str, event_type: ExecutionEventType
) -> list[Any]:
    events = kit.workflow.observe(WORKSPACE, execution_id).events
    return [event for event in events if event.event_type is event_type]


def test_phase_orchestrators_are_children_of_the_workflow_node(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    spawned = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
    )
    node_events = [
        event for event in spawned if event.payload["role"] == WORKFLOW_NODE_ROLE
    ]
    assert len(node_events) == 1
    node_id = str(node_events[0].payload["subAgentId"])
    assert node_events[0].payload["parentSubAgentId"] is None
    phases = [event for event in spawned if event.payload["role"] != WORKFLOW_NODE_ROLE]
    assert [event.payload["phase"] for event in phases] == [
        "hypothesis_creation",
        "falsification_trials",
        "verdict",
        "strategy_design",
        "strategy_backtesting",
        "optimization",
    ]
    for event in phases:
        assert event.payload["subAgentId"]
        assert event.payload["parentSubAgentId"] == node_id
        expected_input = event.payload["phase"] in {"hypothesis_creation", "verdict"}
        assert event.payload["acceptsInput"] is expected_input
        context = cast("dict[str, Any]", event.payload["context"])
        assert context["isolated"] is True
        assert context["knowledge_cutoff"] is None
        assert context["mcp"] == {"enabled": False, "tools": []}
    # Each phase orchestrator records a safe activity label and reply so the
    # nested chat view is never blank without exposing raw model instructions.
    progress = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_PROGRESS
    )
    activities = [
        event
        for event in progress
        if event.payload.get("kind") == "activity" and event.payload.get("text")
    ]
    assert activities
    assert {event.payload["subAgentId"] for event in activities} <= {
        event.payload["subAgentId"] for event in phases
    }
    assert all(
        "Produce the FINAL hypothesis" not in str(event.payload.get("text"))
        for event in activities
    )
    outputs = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_OUTPUT
    )
    assert any(event.payload.get("artifact") for event in outputs)
    completed = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_COMPLETED
    )
    assert {event.payload["subAgentId"] for event in completed} == {
        event.payload["subAgentId"] for event in phases
    } | {node_id}


def test_every_phase_orchestrator_records_its_task_and_summary(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    spawned = [
        event
        for event in events
        if event.event_type is ExecutionEventType.SUB_AGENT_SPAWNED
        and event.payload["parentSubAgentId"] is not None
    ]
    assert spawned
    for spawn in spawned:
        sub_agent_id = spawn.payload["subAgentId"]
        node_events = [
            event for event in events if event.payload.get("subAgentId") == sub_agent_id
        ]
        assert any(
            event.event_type is ExecutionEventType.SUB_AGENT_PROGRESS
            and event.payload.get("kind") == "activity"
            for event in node_events
        ), f"phase {spawn.payload['phase']} records no task"
        assert any(
            event.event_type is ExecutionEventType.SUB_AGENT_OUTPUT
            for event in node_events
        ), f"phase {spawn.payload['phase']} records no summary"


def test_creation_conversation_includes_operator_messages(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            {"message": "Describe the model in more detail.", "advance": False},
            {"message": "The model is ready.", "advance": True},
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(),
        ],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
        prepend_ready=False,
    )
    request = _request(kit)
    kit.workflow.run(request)
    node_id = _creation_node_id(kit, request)
    kit.chat.create_message(
        WORKSPACE,
        request.chat_id,
        actor_id=OWNER,
        body="Restrict the universe to large-cap technology names.",
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        execution_id=request.execution_id,
        data={
            "executionId": request.execution_id,
            "parentSubAgentId": None,
            "phase": "hypothesis_creation",
            "subAgentId": node_id,
        },
    )
    kit.chat.create_message(
        WORKSPACE,
        request.chat_id,
        actor_id=OWNER,
        body="confirm",
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        execution_id=request.execution_id,
        data={
            "executionId": request.execution_id,
            "parentSubAgentId": None,
            "phase": "hypothesis_creation",
            "subAgentId": node_id,
        },
    )

    kit.workflow.resume(request)

    finalize_prompt = _prompt_with(kit, "Produce the FINAL hypothesis")
    assert IDEAS in finalize_prompt
    assert "Restrict the universe to large-cap technology names." in finalize_prompt


def test_creation_conversation_runs_multiple_turns(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            {
                "message": "What is the disagreement with market pricing?",
                "advance": False,
            },
            {"message": "Ready to finalize.", "advance": False},
            {"message": "Confirmation accepted.", "advance": True},
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(),
        ],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
        prepend_ready=False,
    )
    request = _request(kit)

    first = kit.workflow.run(request)
    assert first.phase is HypothesisDevelopmentPhase.PAUSED
    node_id = _creation_node_id(kit, request)

    def answer(body: str) -> None:
        kit.chat.create_message(
            WORKSPACE,
            request.chat_id,
            actor_id=OWNER,
            body=body,
            role=MessageRole.USER,
            kind=TranscriptItemKind.SUB_AGENT,
            execution_id=request.execution_id,
            data={
                "executionId": request.execution_id,
                "parentSubAgentId": None,
                "phase": "hypothesis_creation",
                "subAgentId": node_id,
            },
        )

    answer("The market underreacts to slow information diffusion.")
    second = kit.workflow.resume(request)
    assert second.phase is HypothesisDevelopmentPhase.PAUSED

    answer("confirm")
    final = kit.workflow.resume(request)
    assert final.phase is HypothesisDevelopmentPhase.PAUSED
    assert _latest_input_phase(kit, request) == "verdict"
    final = _answer_current_pause(kit, request)
    assert final.phase is HypothesisDevelopmentPhase.COMPLETED
    # The judged turns, the finalize compose, the plan, the trials analysis, and
    # the design all ran; operator requests are authored by the model as well.
    _prompt_with(kit, "Produce the FINAL hypothesis")
    _prompt_with(kit, "Design falsification trials and negative controls")
    _prompt_with(kit, "falsification trials orchestrator")


def test_model_judges_confirmation_prose_before_advancing(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            {"message": "The model is ready for review.", "advance": False},
            {"message": "That clearly confirms the proposal.", "advance": True},
            _hypothesis_envelope(),
            _plan_envelope(),
            _design_envelope(),
        ],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
        prepend_ready=False,
    )
    request = _request(kit)

    outcome = _run(kit, request, confirm="Looks good to me, please proceed.")

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED


def test_creation_turn_prompt_builds_a_statistical_model_on_history(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    turn_prompt = kit.provider.prompts[0]
    assert IDEAS in turn_prompt
    assert kit.provider.response_formats[0] == {"type": "json_object"}
    # The agent builds the model itself, on history, instead of interrogating
    # the operator about strategy-level details or proposing a forward test.
    assert "ONE statistical model" in turn_prompt
    assert "from HISTORICAL data" in turn_prompt
    assert "never propose a forward-looking experiment" in turn_prompt
    assert "Propose the model yourself" in turn_prompt
    assert "Do not ask the operator which statistics" in turn_prompt
    assert "lead with the proposed model, not with questions" in turn_prompt
    assert "Sizing, costs, borrow, financing, execution" in turn_prompt
    assert "do not ask about them now" in turn_prompt
    # Both falsification layers are explained so the falsifiers serve both.
    assert "Layer A (this pipeline, on history)" in turn_prompt
    assert "Layer B (later, while a position is open)" in turn_prompt
    # The guidance is general across the idea shapes the product supports.
    assert "Two or more assets" in turn_prompt
    assert "Structure of one asset's price action" in turn_prompt
    assert "Options and volatility" in turn_prompt
    assert "negative control" in turn_prompt
    # Math is authored as LaTeX for the KaTeX renderer.
    assert "Render every mathematical expression as LaTeX" in turn_prompt
    assert "inline math as \\(...\\)" in turn_prompt
    # A bare `$` collides with currency, so math never uses it.
    assert "never use a bare $ for math" in turn_prompt
    # Accepting the proposal must not echo the proposed model back to the operator.
    assert "do not restate or re-summarize" in turn_prompt
    assert "Return a single JSON object" in turn_prompt
    assert '"advance": boolean' in turn_prompt


def test_finalize_prompt_requests_the_model_specification(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    _run(kit, request)

    finalize_prompt = _prompt_with(kit, "Produce the FINAL hypothesis")
    assert {"type": "json_object"} in kit.provider.response_formats
    assert "estimated and falsified on historical data" in finalize_prompt
    assert "never with a placeholder" in finalize_prompt
    assert "- claim:" in finalize_prompt
    assert "- disagreement:" in finalize_prompt
    assert "no-structure null" in finalize_prompt
    assert "- falsifiers: the Layer A falsifiers" in finalize_prompt
    assert "double as Layer B monitors" in finalize_prompt
    assert "- body: Markdown with the full model specification" in finalize_prompt
    assert "Render every mathematical expression as LaTeX" in finalize_prompt
    assert "- model (required)" in finalize_prompt
    assert "Never encode an indicator as a strategy" in finalize_prompt
    assert "Omit sizing" in finalize_prompt


def test_later_phases_receive_the_model_and_declared_falsifiers(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    plan_prompt = _prompt_with(
        kit, "binds to one declared falsifier by its falsifierId"
    )
    design_prompt = _prompt_with(
        kit, "re-check the declared falsifiers (by falsifierId)"
    )
    for prompt in (plan_prompt, design_prompt):
        assert "Claim: Five-day momentum in SPY continues" in prompt
        assert "Null / disagreement: The market treats momentum as noise" in prompt
        assert "Universe: SPY" in prompt
        assert (
            "- f1: sharpe lt 0 :: Run the momentum probe and require a negative "
            "Sharpe." in prompt
        )
        assert "Model specification:\nThe continuation thesis." in prompt
    assert "binds to one declared falsifier by its falsifierId" in plan_prompt
    assert "Layer B monitor" in design_prompt
    assert "re-check the declared falsifiers (by falsifierId)" in design_prompt


def test_workflow_advances_without_gate_events(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert (
        _events_by_type(
            kit, request.execution_id, ExecutionEventType.WORKFLOW_GATE_BLOCKED
        )
        == []
    )
    assert (
        _events_by_type(
            kit, request.execution_id, ExecutionEventType.WORKFLOW_GATE_RESOLVED
        )
        == []
    )


def test_unfalsifiable_hypothesis_marks_orchestrator_as_needing_input(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(falsifiers=[])],
        results=[],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.detail is not None
    assert "falsifier" in outcome.detail
    needs_input = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    )
    assert needs_input
    assert needs_input[-1].payload["acceptsInput"] is True
    assert needs_input[-1].payload["question"]
    tree = SubAgentTree.from_events(
        request.execution_id,
        kit.workflow.observe(WORKSPACE, request.execution_id).events,
    )
    orchestrator = tree.get(cast(str, needs_input[-1].payload["subAgentId"]))
    assert orchestrator is not None
    assert orchestrator.accepts_input is True
    assert orchestrator.status == "needs_input"


def test_launch_appends_a_workflow_anchor_transcript_item(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)

    messages = kit.chat.list_messages(WORKSPACE, request.chat_id)
    anchors = [
        message for message in messages if message.kind is TranscriptItemKind.WORKFLOW
    ]
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.role.value == "system"
    assert anchor.execution_id == request.execution_id
    assert anchor.data is not None
    assert anchor.data["executionId"] == request.execution_id
    assert anchor.data["workflowId"] == "hypothesis-development"
    assert anchor.data["version"] == "1.0.0"
    assert anchor.data["title"] == "Hypothesis development"
    assert anchor.data["status"] == "running"
    assert anchor.data["currentPhase"] == "requested"


class _StubInvocation:
    """Minimal invocation stand-in exposing only the composer input surface."""

    def __init__(self, output: str) -> None:
        self.response = ModelResponse(output=output)


def _fenced(envelope: dict[str, object]) -> str:
    return f"```json\n{json.dumps(envelope)}\n```"


def test_fenced_hypothesis_output_advances_the_pipeline(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _fenced(_hypothesis_envelope()),
            _plan_envelope(),
            _design_envelope(),
        ],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert any(
        "Respond with a single JSON object and nothing else" in prompt
        for prompt in kit.provider.prompts
    )


def test_prose_wrapped_hypothesis_output_advances_the_pipeline(tmp_path: Path) -> None:
    prose = (
        "Sure, here is the hypothesis:\n"
        f"{json.dumps(_hypothesis_envelope())}\n"
        "Let me know if you need changes."
    )
    kit = _kit(
        tmp_path,
        payloads=[prose, _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED


@pytest.mark.parametrize(
    "output",
    [
        json.dumps(_hypothesis_envelope()),
        _fenced(_hypothesis_envelope()),
        "```\n" + json.dumps(_hypothesis_envelope()) + "\n```",
        f"prose before {json.dumps(_hypothesis_envelope())} prose after",
    ],
)
def test_json_document_extracts_tolerant_hypothesis_output(output: str) -> None:
    invocation = cast("InvocationResult", _StubInvocation(output))

    document = _json_document(invocation, "hypothesis")

    assert document["claim"] == _hypothesis_envelope()["claim"]


def test_json_document_rejects_non_json_output() -> None:
    invocation = cast("InvocationResult", _StubInvocation("no structure here"))

    with pytest.raises(FalsificationComposeError, match="not valid JSON"):
        _json_document(invocation, "hypothesis")


def test_non_json_hypothesis_output_repairs_then_pauses_for_input(
    tmp_path: Path,
) -> None:
    bad = "The model refused to produce structured output."
    kit = _kit(tmp_path, payloads=[bad, bad, bad], results=[])
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.detail is not None
    assert "JSON" in outcome.detail
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.PAUSED

    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    event_types = {event.event_type for event in events}
    assert ExecutionEventType.WORKFLOW_FAILED not in event_types
    assert ExecutionEventType.WORKFLOW_PAUSED in event_types
    needs_input = [
        event
        for event in events
        if event.event_type is ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    ]
    # Confirmation is always explicit; malformed finalization opens a second pause.
    assert len(needs_input) == 2
    assert needs_input[-1].payload["acceptsInput"] is True
    assert needs_input[-1].payload["question"]


def test_non_json_output_reinvokes_the_model_with_error_and_schema(
    tmp_path: Path,
) -> None:
    bad = "not json at all"
    kit = _kit(tmp_path, payloads=[bad, bad, bad], results=[])
    request = _request(kit)

    _run(kit, request)

    # The creation turn, the repair re-invocation, then the plan and design.
    repair_prompt = _prompt_with(kit, "not valid JSON")
    assert '"claim"' in repair_prompt
    assert {"type": "json_object"} in kit.provider.response_formats


def test_optional_hypothesis_fields_default_and_compose(tmp_path: Path) -> None:
    minimal: dict[str, object] = {
        "claim": "Five-day momentum in SPY continues over the next five days.",
        "disagreement": "The market underprices slow information diffusion.",
        "falsifiers": [
            {
                "observable": "sharpe",
                "condition": "lt",
                "threshold": 0.0,
                "test": "Run the probe and require a negative Sharpe.",
            }
        ],
        "model": _model(),
    }
    kit = _kit(
        tmp_path,
        payloads=[minimal, _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert outcome.hypothesis is not None
    front = outcome.hypothesis.front_matter
    assert front.horizon == "unspecified"
    assert front.risk_factor == "unspecified"
    assert front.universe == ("SPY",)
    assert front.falsifiers[0].falsifier_id == "f1"


def test_metric_accepts_catalog_strings_and_nested_custom_values() -> None:
    assert _metric({"rolling_corr": "0.73"}, "rolling_corr") == 0.73
    assert _metric({"custom": {"rolling_corr": "0.81"}}, "rolling_corr") == 0.81
    assert _metric({"rolling_corr": True}, "rolling_corr") is None
    assert _metric({"rolling_corr": "NaN"}, "rolling_corr") is None
    assert _metric({"rolling_corr": "not-a-number"}, "rolling_corr") is None


def test_launch_is_deterministic_and_never_calls_the_model(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])

    request = _request(kit)

    assert kit.provider.prompts == []
    assert kit.provider.response_formats == []
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.workflow_id == "hypothesis-development"
    assert execution.trigger_message_id == request.trigger_message_id


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


def test_unresolved_model_run_pauses_instead_of_falsifying_every_trial(
    tmp_path: Path,
) -> None:
    research_runner = _TimingOutResearchRunner()
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope()],
        results=[],
        research_runner=research_runner,
    )
    request = _request(kit)

    outcome = _run(kit, request)

    # The run is still executing, so every trial is unresolved rather than
    # refuted: the workflow pauses for the operator instead of stamping T01-T11
    # as failed with no observation.
    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert _latest_input_phase(kit, request) == "falsification_trials"
    assert len(research_runner.requests) == 1
    trials = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    )
    assert "terminal state" in str(trials[-1].payload.get("question", ""))


def test_long_horizon_and_crlf_newlines_finalize_successfully(tmp_path: Path) -> None:
    envelope = _hypothesis_envelope()
    long_horizon = (
        "Synchronized 5-minute regular-session returns for MSTR and IBIT from IBIT "
        "inception through the latest complete session, with QQQ as a broad-equity "
        "control. Full-sample estimation plus trailing 20-session monitoring windows "
        "containing at least 500 bars; 15-minute bars are evaluated as a robustness "
        "alternative.\r\nDetailed sampling window notes."
    )
    assert len(long_horizon) > 256
    envelope["horizon"] = long_horizon
    kit = _kit(
        tmp_path,
        payloads=[envelope, _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert outcome.hypothesis is not None
    assert outcome.hypothesis.front_matter.horizon.startswith("Synchronized 5-minute")


def test_workflow_node_is_durable_and_owns_every_phase_agent(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    node = _workflow_node(kit, request)
    assert node.parent_sub_agent_id is None
    assert node.workflow_execution_id == request.execution_id

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    records = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    phases = [record for record in records if record.role != WORKFLOW_NODE_ROLE]
    assert phases
    assert all(record.parent_sub_agent_id == node.sub_agent_id for record in phases)
    lifecycle = SubAgentLifecycle(
        kit.chat, store=PersistenceSubAgentStore(kit.chat.persistence)
    )
    stored = lifecycle.workflow_node(WORKSPACE, request.execution_id)
    assert stored is not None
    assert stored.sub_agent_id == node.sub_agent_id
    assert stored.status is SubAgentStatus.COMPLETED


def test_node_launched_execution_is_owned_by_its_anchor(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="anchored chat", owner_actor_id=OWNER
    ).chat_id
    lifecycle = SubAgentLifecycle(
        kit.chat, store=PersistenceSubAgentStore(kit.chat.persistence)
    )
    anchor = lifecycle.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.CONVERSATION),
        actor_id=OWNER,
    )
    trigger = kit.chat.create_message(WORKSPACE, chat_id, actor_id=OWNER, body=IDEAS)

    request = kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        ideas=IDEAS,
        trigger_message_id=trigger.message_id,
        anchor_sub_agent_id=anchor.sub_agent_id,
    )

    node = _workflow_node(kit, request)
    assert node.parent_sub_agent_id == anchor.sub_agent_id
    assert kit.chat.list_executions(WORKSPACE) == ()
    assert kit.chat.list_node_executions(WORKSPACE, anchor.sub_agent_id) == (
        kit.chat.get_execution(WORKSPACE, request.execution_id),
    )
    assert (
        kit.chat.execution_anchor(WORKSPACE, request.execution_id)
        == anchor.sub_agent_id
    )
    anchors = [
        message
        for message in kit.chat.list_messages(WORKSPACE, chat_id)
        if message.kind is TranscriptItemKind.WORKFLOW
    ]
    assert len(anchors) == 1
    assert anchors[0].data is not None
    assert anchors[0].data["subAgentId"] == anchor.sub_agent_id

    outcome = _run(kit, request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    records = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    phases = [
        record
        for record in records
        if record.role != WORKFLOW_NODE_ROLE
        and record.workflow_execution_id == request.execution_id
    ]
    assert phases
    assert all(record.parent_sub_agent_id == node.sub_agent_id for record in phases)


def test_workflow_node_guards_reject_over_deep_crowded_and_over_budget_agents(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="guarded chat", owner_actor_id=OWNER
    ).chat_id
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    anchor = lifecycle.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.CONVERSATION),
        actor_id=OWNER,
    )
    trigger = kit.chat.create_message(WORKSPACE, chat_id, actor_id=OWNER, body=IDEAS)
    request = kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        ideas=IDEAS,
        trigger_message_id=trigger.message_id,
        anchor_sub_agent_id=anchor.sub_agent_id,
    )
    node = _workflow_node(kit, request)

    depth_guarded = SubAgentLifecycle(kit.chat, store=store, max_depth=1)
    with pytest.raises(SubAgentLifecycleError, match="depth"):
        depth_guarded.spawn(
            WORKSPACE,
            chat_id,
            spec=SubAgentSpec(role="phase"),
            actor_id=OWNER,
            parent_sub_agent_id=node.sub_agent_id,
        )
    budget_guarded = SubAgentLifecycle(kit.chat, store=store, max_depth=4)
    with pytest.raises(SubAgentLifecycleError, match="budget"):
        budget_guarded.spawn(
            WORKSPACE,
            chat_id,
            spec=SubAgentSpec(role="phase", delegated_budget=1),
            actor_id=OWNER,
            parent_sub_agent_id=node.sub_agent_id,
        )
    children_guarded = SubAgentLifecycle(
        kit.chat, store=store, max_depth=4, max_children=1
    )
    children_guarded.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="phase"),
        actor_id=OWNER,
        parent_sub_agent_id=node.sub_agent_id,
    )
    with pytest.raises(SubAgentLifecycleError, match="active children"):
        children_guarded.spawn(
            WORKSPACE,
            chat_id,
            spec=SubAgentSpec(role="phase"),
            actor_id=OWNER,
            parent_sub_agent_id=node.sub_agent_id,
        )


def test_depth_guarded_phase_spawn_fails_without_orphans(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[], max_depth=1)
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="shallow chat", owner_actor_id=OWNER
    ).chat_id
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    anchor = lifecycle.spawn(
        WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.CONVERSATION),
        actor_id=OWNER,
    )
    trigger = kit.chat.create_message(WORKSPACE, chat_id, actor_id=OWNER, body=IDEAS)
    request = kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        ideas=IDEAS,
        trigger_message_id=trigger.message_id,
        anchor_sub_agent_id=anchor.sub_agent_id,
    )

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.FAILED
    node = _workflow_node(kit, request)
    assert node.status is SubAgentStatus.FAILED
    records = store.list_sub_agents(WORKSPACE)
    assert {record.sub_agent_id for record in records} == {
        anchor.sub_agent_id,
        node.sub_agent_id,
    }


def test_launch_requires_the_requesting_message_and_never_synthesizes_one(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="no trigger", owner_actor_id=OWNER
    ).chat_id

    with pytest.raises(HypothesisDevelopmentIntakeError, match="trigger_message_id"):
        kit.workflow.launch(WORKSPACE, chat_id, actor_id=OWNER, ideas=IDEAS)

    assert kit.chat.list_executions(WORKSPACE) == ()
    assert kit.chat.list_messages(WORKSPACE, chat_id) == ()


def test_launch_binds_to_the_requesting_message_without_a_duplicate(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="bound trigger", owner_actor_id=OWNER
    ).chat_id
    trigger = kit.chat.create_message(WORKSPACE, chat_id, actor_id=OWNER, body=IDEAS)

    request = kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        ideas=IDEAS,
        trigger_message_id=trigger.message_id,
    )

    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.trigger_message_id == trigger.message_id
    user_messages = [
        message
        for message in kit.chat.list_messages(WORKSPACE, chat_id)
        if message.role is MessageRole.USER
    ]
    assert [message.message_id for message in user_messages] == [trigger.message_id]


def test_resumed_phase_reuses_the_same_workflow_node(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    first = kit.workflow.run(request)
    assert first.phase is HypothesisDevelopmentPhase.PAUSED
    second = _answer_current_pause(kit, request)
    assert second.phase is HypothesisDevelopmentPhase.PAUSED
    assert _latest_input_phase(kit, request) == "verdict"

    node = _workflow_node(kit, request)
    final = _answer_current_pause(kit, request)

    assert final.phase is HypothesisDevelopmentPhase.COMPLETED
    records = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    assert len([record for record in records if record.role == WORKFLOW_NODE_ROLE]) == 1
    verdict_spawns = [
        event
        for event in _events_by_type(
            kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
        )
        if event.payload["phase"] == "verdict"
    ]
    assert len(verdict_spawns) == 1
    assert verdict_spawns[0].payload["parentSubAgentId"] == node.sub_agent_id
