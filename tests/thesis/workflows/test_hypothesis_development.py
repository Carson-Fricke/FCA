from __future__ import annotations

import json
from collections.abc import Callable
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
from thesis.agent.skills import SkillRegistry
from thesis.agent.subagents import SubagentContext, SubagentContextFactory
from thesis.chat.contracts import (
    MessageRole,
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
from thesis.workflows.backtests import BacktestRunRequest, BacktestRunResult
from thesis.workflows.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_PHASES,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    FalsificationComposeError,
    HypothesisDevelopmentPhase,
    HypothesisDevelopmentWorkflow,
    InvalidPhaseTransition,
    _json_document,
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


def _probe() -> dict[str, object]:
    return {
        "strategyId": "momentum-probe",
        "version": "1.0.0",
        "files": {
            "strategy.py": "from harness.strategy import Strategy\n",
        },
        "tests": {"test_strategy.py": "def test_strategy():\n    assert True\n"},
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
        "probe": _probe(),
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
        if not self._payloads:
            raise AssertionError("model invoked more times than expected")
        payload = self._payloads.pop(0)
        output = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(output=output)


class FakeRunner:
    """Deterministic fake runner returning queued ``(state, metrics)`` pairs."""

    def __init__(self, results: list[tuple[str, object | None]]) -> None:
        self._results = list(results)
        self.requests: list[BacktestRunRequest] = []

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        state, metrics = self._results.pop(0)
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
    results: list[tuple[str, object | None]],
    exit_criteria: list[dict[str, object]] | None = None,
) -> _Kit:
    clock = _clock()
    id_factory = _id_factory()
    chat = ChatService(clock=clock, id_factory=id_factory)
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
    runner = FakeRunner(results)
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
    return kit.workflow.launch(WORKSPACE, chat_id, actor_id=OWNER, ideas=ideas)


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

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert outcome.fenced is False
    assert outcome.verdict is not None
    assert outcome.verdict.outcome == VERDICT_SUPPORTED
    assert outcome.strategy_bundle is not None
    assert outcome.optimization_backtest is not None
    assert outcome.journal_id == f"workflow-{request.execution_id}"
    assert [item.to_phase for item in outcome.transitions] == [
        HypothesisDevelopmentPhase.IDEATION,
        HypothesisDevelopmentPhase.HYPOTHESIS,
        HypothesisDevelopmentPhase.FALSIFICATION_PLAN,
        HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
        HypothesisDevelopmentPhase.VERDICT,
        HypothesisDevelopmentPhase.STRATEGY_DESIGN,
        HypothesisDevelopmentPhase.OPTIMIZATION,
        HypothesisDevelopmentPhase.COMPLETED,
    ]
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    assert len(kit.runner.requests) == 2

    stored = kit.store.load("momentum-breakout")
    assert stored.status is HypothesisStatus.SUPPORTED
    assert stored.front_matter.links.runs == ("job-1",)
    assert stored.front_matter.links.evidence
    assert kit.ledger.entries()
    kit.ledger.verify()
    kit.journal.verify_integrity()


def test_each_phase_emits_a_workflow_phase_event(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    kit.workflow.run(request)

    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    phases = [
        event.payload["phase"]
        for event in replay.events
        if event.event_type is ExecutionEventType.WORKFLOW_PHASE
    ]
    assert phases == [
        "ideation",
        "hypothesis",
        "falsification_plan",
        "falsification_trials",
        "verdict",
        "strategy_design",
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

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.detail is not None
    assert "falsifier" in outcome.detail
    assert kit.runner.requests == []
    assert kit.store.load("momentum-breakout").status is HypothesisStatus.ACTIVE


def test_falsification_plan_gate_pauses_without_a_control(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(controls=[])],
        results=[],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.detail is not None
    assert "control" in outcome.detail
    assert kit.runner.requests == []


def test_verdict_gate_returns_refuted_premise_to_hypothesis(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope()],
        results=[("completed", {"sharpe": -0.5})],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.verdict is not None
    assert outcome.verdict.outcome == VERDICT_REFUTED
    assert outcome.detail is not None
    assert "refuted" in outcome.detail
    assert outcome.strategy_bundle is None
    assert kit.store.load("momentum-breakout").status is HypothesisStatus.REFUTED


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

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.detail is not None
    assert "exit criterion" in outcome.detail
    assert outcome.optimization_backtest is None


def test_rca_fans_out_fresh_subagents_and_retries(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            {
                "falsifierId": "f1",
                "failureClass": "harness",
                "rationale": "The trial run aborted before recording metrics.",
                "confidence": 0.7,
                "followUps": ["Re-run the unchanged probe on a clean worker."],
            },
            _design_envelope(),
        ],
        results=[
            ("aborted", None),
            ("completed", {"sharpe": 1.5}),
            ("completed", {"sharpe": 1.2}),
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert len(outcome.rca) == 1
    assert outcome.rca[0].failure_class == "harness"
    assert len(kit.factory.created) == 1
    assert kit.factory.created[0].transcript is None
    assert len(kit.runner.requests) == 3
    phases = [item.to_phase for item in outcome.transitions]
    assert HypothesisDevelopmentPhase.RCA in phases
    assert phases.count(HypothesisDevelopmentPhase.FALSIFICATION_TRIALS) == 2


def test_phase_transition_table_is_explicit_and_auditable() -> None:
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.REQUESTED,
            HypothesisDevelopmentPhase.IDEATION,
        )
        is HypothesisDevelopmentPhase.IDEATION
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
            HypothesisDevelopmentPhase.RCA,
        )
        is HypothesisDevelopmentPhase.RCA
    )
    assert (
        transition_phase(
            HypothesisDevelopmentPhase.RCA,
            HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
        )
        is HypothesisDevelopmentPhase.FALSIFICATION_TRIALS
    )
    with pytest.raises(InvalidPhaseTransition, match="requested to verdict"):
        transition_phase(
            HypothesisDevelopmentPhase.REQUESTED,
            HypothesisDevelopmentPhase.VERDICT,
        )
    with pytest.raises(InvalidPhaseTransition, match="completed to hypothesis"):
        transition_phase(
            HypothesisDevelopmentPhase.COMPLETED,
            HypothesisDevelopmentPhase.HYPOTHESIS,
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
    assert registry.resolve(HYPOTHESIS_AUTHORING_SKILL_ID).version == "1.0.0"
    assert registry.resolve(FALSIFICATION_RCA_SKILL_ID).version == "1.0.0"

    research = with_hypothesis_development_skills(
        RolePolicy(role="research", version="1.0.0", model_profile="research-profile")
    )
    assert _skill_keys(research) == {
        "hypothesis-authoring@1.0.0",
        "falsification-planning@1.0.0",
    }
    coding = with_hypothesis_development_skills(
        RolePolicy(role="coding", version="1.0.0", model_profile="coding-profile")
    )
    assert _skill_keys(coding) == {STRATEGY_AUTHORING_SKILL_ID + "@1.0.0"}
    primary = with_hypothesis_development_skills(
        RolePolicy(role="primary", version="1.0.0", model_profile="primary-profile")
    )
    assert primary.skills == ()


def _events_by_type(
    kit: _Kit, execution_id: str, event_type: ExecutionEventType
) -> list[Any]:
    events = kit.workflow.observe(WORKSPACE, execution_id).events
    return [event for event in events if event.event_type is event_type]


def test_phase_orchestrators_are_top_level_sub_agents(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    kit.workflow.run(request)

    spawned = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_SPAWNED
    )
    top_level = [
        event for event in spawned if event.payload["parentSubAgentId"] is None
    ]
    assert [event.payload["phase"] for event in top_level] == [
        "ideation",
        "hypothesis",
        "falsification_plan",
        "falsification_trials",
        "verdict",
        "strategy_design",
        "optimization",
    ]
    for event in top_level:
        assert event.payload["subAgentId"]
        expected_input = event.payload["phase"] == "ideation"
        assert event.payload["acceptsInput"] is expected_input
        context = cast("dict[str, Any]", event.payload["context"])
        assert context["isolated"] is True
        assert context["knowledge_cutoff"] is None
        assert context["mcp"] == {"enabled": False, "tools": []}
    completed = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_COMPLETED
    )
    assert {event.payload["subAgentId"] for event in completed} == {
        event.payload["subAgentId"] for event in top_level
    }


def test_ideation_incorporates_operator_messages_sent_to_the_ideation_node(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)
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
            "phase": "ideation",
            "subAgentId": "subagent-ideation",
        },
    )

    kit.workflow.run(request)

    ideation_prompt = kit.provider.prompts[0]
    assert IDEAS in ideation_prompt
    assert "Restrict the universe to large-cap technology names." in ideation_prompt


def test_nested_rca_sub_agent_links_to_its_phase_orchestrator(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[
            _hypothesis_envelope(),
            _plan_envelope(),
            {
                "falsifierId": "f1",
                "failureClass": "harness",
                "rationale": "The trial run aborted before recording metrics.",
                "confidence": 0.7,
                "followUps": ["Re-run the unchanged probe on a clean worker."],
            },
            _design_envelope(),
        ],
        results=[
            ("aborted", None),
            ("completed", {"sharpe": 1.5}),
            ("completed", {"sharpe": 1.2}),
        ],
    )
    request = _request(kit)

    kit.workflow.run(request)

    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    spawned = {
        event.payload["subAgentId"]: event
        for event in events
        if event.event_type is ExecutionEventType.SUB_AGENT_SPAWNED
    }
    nested = [
        event
        for event in spawned.values()
        if event.payload["parentSubAgentId"] is not None
    ]
    assert len(nested) == 1
    child = nested[0]
    assert child.payload["phase"] == "rca"
    parent_id = child.payload["parentSubAgentId"]
    assert isinstance(parent_id, str)
    parent = spawned[parent_id]
    assert parent.payload["phase"] == "rca"
    assert parent.payload["parentSubAgentId"] is None
    assert kit.factory.created[0].context_id == child.payload["subAgentId"]
    child_events = [
        event
        for event in events
        if event.payload.get("subAgentId") == child.payload["subAgentId"]
    ]
    assert {event.event_type for event in child_events} == {
        ExecutionEventType.SUB_AGENT_SPAWNED,
        ExecutionEventType.SUB_AGENT_PROGRESS,
        ExecutionEventType.SUB_AGENT_OUTPUT,
        ExecutionEventType.SUB_AGENT_COMPLETED,
    }
    output = next(
        event
        for event in child_events
        if event.event_type is ExecutionEventType.SUB_AGENT_OUTPUT
    )
    artifact = cast("dict[str, Any]", output.payload["artifact"])
    assert artifact["type"] == "rca_diagnosis"
    progress = next(
        event
        for event in child_events
        if event.event_type is ExecutionEventType.SUB_AGENT_PROGRESS
    )
    assert progress.payload["kind"] == "thinking"
    assert progress.payload["text"]


def test_satisfied_gates_emit_resolved_events(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(), _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    kit.workflow.run(request)

    resolved = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_GATE_RESOLVED
    )
    assert [event.payload["gate"] for event in resolved] == [
        "falsifiability",
        "falsification_plan",
        "evidence_bound",
        "strategy_design",
    ]
    assert (
        _events_by_type(
            kit, request.execution_id, ExecutionEventType.WORKFLOW_GATE_BLOCKED
        )
        == []
    )


def test_blocked_gate_marks_orchestrator_as_needing_input(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[_hypothesis_envelope(falsifiers=[])],
        results=[],
    )
    request = _request(kit)

    kit.workflow.run(request)

    blocked = _events_by_type(
        kit, request.execution_id, ExecutionEventType.WORKFLOW_GATE_BLOCKED
    )
    assert len(blocked) == 1
    assert blocked[0].payload["gate"] == "falsifiability"
    needs_input = _events_by_type(
        kit, request.execution_id, ExecutionEventType.SUB_AGENT_NEEDS_INPUT
    )
    assert len(needs_input) == 1
    assert needs_input[0].payload["acceptsInput"] is True
    assert needs_input[0].payload["question"]
    tree = SubAgentTree.from_events(
        request.execution_id,
        kit.workflow.observe(WORKSPACE, request.execution_id).events,
    )
    orchestrator = tree.get(cast(str, needs_input[0].payload["subAgentId"]))
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
    assert anchor.data["status"] == "queued"
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

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert (
        "Respond with a single JSON object and nothing else" in kit.provider.prompts[0]
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

    outcome = kit.workflow.run(request)

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

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.detail is not None
    assert "falsifier" in outcome.detail
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
    assert len(needs_input) == 1
    assert needs_input[0].payload["acceptsInput"] is True
    assert needs_input[0].payload["question"]


def test_non_json_output_reinvokes_the_model_with_error_and_schema(
    tmp_path: Path,
) -> None:
    bad = "not json at all"
    kit = _kit(tmp_path, payloads=[bad, bad, bad], results=[])
    request = _request(kit)

    kit.workflow.run(request)

    assert len(kit.provider.prompts) == 3
    assert "not valid JSON" in kit.provider.prompts[1]
    assert '"claim"' in kit.provider.prompts[1]
    assert all(
        item == {"type": "json_object"} for item in kit.provider.response_formats
    )


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
    }
    kit = _kit(
        tmp_path,
        payloads=[minimal, _plan_envelope(), _design_envelope()],
        results=[("completed", {"sharpe": 1.5}), ("completed", {"sharpe": 1.2})],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisDevelopmentPhase.COMPLETED
    assert outcome.hypothesis is not None
    front = outcome.hypothesis.front_matter
    assert front.horizon == "unspecified"
    assert front.risk_factor == "unspecified"
    assert front.universe == ("UNSPECIFIED",)
    assert front.falsifiers[0].falsifier_id == "f1"


def test_launch_is_deterministic_and_never_calls_the_model(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])

    request = _request(kit)

    assert kit.provider.prompts == []
    assert kit.provider.response_formats == []
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.workflow_id == "hypothesis-development"
    assert execution.trigger_message_id == request.trigger_message_id
