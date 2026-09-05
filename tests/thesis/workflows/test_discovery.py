from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import cast

import pytest

from thesis.agent.context import PromptContext
from thesis.agent.router import (
    ModelProfile,
    ModelResponse,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.search_policy import (
    BudgetUsage,
    DiscoveryBudget,
    DiscoveryCandidate,
    RationaleKind,
    SearchPolicy,
    SearchPolicyError,
    SearchRationale,
)
from thesis.agent.skills import SkillRegistry, ThesisSkill
from thesis.chat.contracts import WorkflowExecutionState
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.persistence import CommandJournal
from thesis.strategies.validation import (
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows.backtests import BacktestRunRequest, BacktestRunResult
from thesis.workflows.discovery import (
    DISCOVERY_DISCLOSURE,
    DISCOVERY_PHASES,
    DISCOVERY_WORKFLOW,
    MULTIPLE_TESTING_DISCLOSURE,
    AttemptKind,
    DiscoveryOutcomeKind,
    DiscoveryPhase,
    DiscoveryRequest,
    DiscoveryWorkflow,
    InMemoryAttemptLedger,
    InvalidPhaseTransition,
    TriageOutcome,
    TriageRequest,
    TriageResult,
    transition_phase,
)

WORKSPACE = "workspace-demo"
OWNER = "owner-1"
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
STRATEGY_FILE = (
    "from harness.strategy import Strategy\n\nclass Momentum(Strategy):\n    pass\n"
)
TEST_FILE = "def test_strategy():\n    assert True\n"


def _clock() -> Callable[[], datetime]:
    ticks = count(1)

    def clock() -> datetime:
        return NOW.replace(microsecond=next(ticks))

    return clock


def _id_factory() -> Callable[[str], str]:
    counter = count(1)
    return lambda prefix: f"{prefix}-{next(counter)}"


def _candidate(
    candidate_id: str = "c-momentum",
    *,
    market: str = "equities-us",
    horizon: str = "daily",
    direction: str = "momentum",
    cost: int = 2,
    files: bool = True,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "candidateId": candidate_id,
        "hypothesis": "Short-term momentum persists in US equities",
        "market": market,
        "horizon": horizon,
        "direction": direction,
        "rationale": {
            "kind": "economic-premise",
            "statement": "Underreaction to news creates return drift",
            "references": ["doc-1"],
        },
        "estimatedCost": cost,
        "harnessConfiguration": {"symbols": ["SPY"]},
    }
    if files:
        entry["files"] = {"strategy.py": STRATEGY_FILE}
        entry["tests"] = {"test_strategy.py": TEST_FILE}
    return entry


class FakeProvider:
    """Deterministic fake model provider behind the policy boundary.

    Payloads are returned one per invocation in order; once the list is
    exhausted the provider returns an empty candidate envelope, which ends
    generation deterministically.
    """

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.prompts: list[str] = []
        self.steer: Callable[[], object] | None = None

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
    ) -> ModelResponse:
        self.prompts.append(prompt.rendered)
        if self.steer is not None:
            steer_once = self.steer
            self.steer = None
            steer_once()
        index = len(self.prompts) - 1
        payload = (
            self.payloads[index] if index < len(self.payloads) else {"candidates": []}
        )
        return ModelResponse(output=json.dumps(payload))


class RecordingValidator:
    def __init__(
        self,
        *,
        accepted: bool = True,
        diagnostics: tuple[str, ...] = (),
    ) -> None:
        self.accepted = accepted
        self.diagnostics = diagnostics
        self.requests: list[StrategyValidationRequest] = []

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        self.requests.append(request)
        return StrategyValidationResult(
            self.accepted, self.diagnostics, validation_id="validation-1"
        )


class FakeRunner:
    """Deterministic fake backtest runner behind the runner port."""

    def __init__(self, *, states: list[str] | None = None) -> None:
        self.requests: list[BacktestRunRequest] = []
        self.states = states or []
        self._calls = 0

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        self._calls += 1
        state = self.states.pop(0) if self.states else "completed"
        return BacktestRunResult(
            job_id=f"job-{self._calls}",
            state=state,
            mode="local",
            metrics={"sharpe": 1.25, "totalReturn": 0.08},
        )


class FakeTriage:
    """Deterministic fake cheap-check runner behind the triage port."""

    def __init__(self, outcomes: dict[str, str] | None = None) -> None:
        self.requests: list[TriageRequest] = []
        self.outcomes = outcomes or {}

    def run(self, request: TriageRequest) -> TriageResult:
        self.requests.append(request)
        outcome = cast("TriageOutcome", self.outcomes.get(request.stage, "passed"))
        return TriageResult(
            stage=request.stage,
            outcome=outcome,
            diagnostics=() if outcome == "passed" else ("signal is degenerate",),
        )


class _Kit:
    def __init__(
        self,
        *,
        workflow: DiscoveryWorkflow,
        chat: ChatService,
        journal: CommandJournal | None,
        provider: FakeProvider,
        validator: RecordingValidator,
        runner: FakeRunner,
        triage: FakeTriage,
        ledger: InMemoryAttemptLedger,
        policy: SearchPolicy,
    ) -> None:
        self.workflow = workflow
        self.chat = chat
        self.journal = journal
        self.provider = provider
        self.validator = validator
        self.runner = runner
        self.triage = triage
        self.ledger = ledger
        self.policy = policy


def _workflow(
    tmp_path,
    *,
    payloads: list[dict[str, object]] | None = None,
    validator: RecordingValidator | None = None,
    runner: FakeRunner | None = None,
    triage: FakeTriage | None = None,
    budget: DiscoveryBudget | None = None,
    with_journal: bool = True,
) -> _Kit:
    clock = _clock()
    id_factory = _id_factory()
    chat = ChatService(clock=clock, id_factory=id_factory)
    selected_provider = FakeProvider(
        payloads if payloads is not None else [{"candidates": [_candidate()]}]
    )
    skills = SkillRegistry(
        (
            ThesisSkill(
                "research",
                "1.0.0",
                "Ground every candidate in an economic rationale.",
                ("read_workspace",),
            ),
        )
    )
    profiles = {
        "research-profile": ModelProfile(
            profile_id="research-profile",
            version="1.0.0",
            provider="fake",
            model="research-model",
        )
    }
    policies: dict[Role, RolePolicy] = {
        "research": RolePolicy(
            role="research",
            version="1.0.0",
            model_profile="research-profile",
            skills=("research",),
            allowed_tools=("read_workspace",),
        )
    }
    router = ModelRouter(
        role_policies=policies,
        model_profiles=profiles,
        providers={"fake": selected_provider},
        skills=skills,
        bundle=PolicyBundle(
            bundle_id="thesis-platform",
            version="1.0.0",
            bundle_sha256="a" * 64,
            policy_sha256="b" * 64,
        ),
        clock=clock,
        id_factory=lambda: id_factory("invocation"),
    )
    selected_validator = validator if validator is not None else RecordingValidator()
    selected_runner = runner if runner is not None else FakeRunner()
    selected_triage = triage if triage is not None else FakeTriage()
    selected_budget = budget or DiscoveryBudget(
        max_candidates=4, max_model_invocations=4, max_backtests=4
    )
    policy = SearchPolicy(selected_budget, seed=7)
    ledger = InMemoryAttemptLedger()
    journal = CommandJournal(tmp_path / "journal.sqlite3") if with_journal else None
    workflow = DiscoveryWorkflow(
        chat=chat,
        router=router,
        validator=selected_validator,
        runner=selected_runner,
        triage=selected_triage,
        policy=policy,
        ledger=ledger,
        journal=journal,
        clock=clock,
        id_factory=id_factory,
        role="research",
        experiment_id="experiment-1",
    )
    return _Kit(
        workflow=workflow,
        chat=chat,
        journal=journal,
        provider=selected_provider,
        validator=selected_validator,
        runner=selected_runner,
        triage=selected_triage,
        ledger=ledger,
        policy=policy,
    )


def _request(kit: _Kit) -> DiscoveryRequest:
    chat = kit.chat
    chat_id = chat.create_chat(
        WORKSPACE, title="discovery chat", owner_actor_id=OWNER
    ).chat_id
    return kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        objective="Discover momentum strategies within the workspace charter",
        markets=("equities-us",),
        horizons=("daily",),
        directions=("momentum", "mean-reversion"),
    )


def test_discovery_executes_documented_path_end_to_end(tmp_path: Path) -> None:
    kit = _workflow(
        tmp_path,
        payloads=[
            {
                "candidates": [
                    _candidate("c-expensive", cost=3),
                    _candidate("c-cheap", cost=1),
                ]
            }
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.COMPLETED
    assert outcome.fenced is False
    assert [item.to_phase for item in outcome.transitions] == [
        DiscoveryPhase.SCOPING,
        DiscoveryPhase.GENERATING,
        DiscoveryPhase.RANKING,
        DiscoveryPhase.TRIAGING,
        DiscoveryPhase.BACKTESTING,
        DiscoveryPhase.TRIAGING,
        DiscoveryPhase.BACKTESTING,
        DiscoveryPhase.RECORDING,
        DiscoveryPhase.COMPLETED,
    ]
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    assert execution.finished_at is not None
    assert len(execution.tool_job_ids) == 2
    report = outcome.report
    assert report is not None
    assert report.outcome is DiscoveryOutcomeKind.IMPLEMENTED
    assert report.counts == {
        "generated": 2,
        "ranked": 2,
        "rejected": 0,
        "aborted": 0,
        "implemented": 2,
    }
    assert report.usage.backtests == 2
    assert report.generation_rounds == 2
    assert report.empty_generation_rounds == 1
    assert [entry["candidateId"] for entry in report.implemented] == [
        "c-cheap",
        "c-expensive",
    ]
    assert report.schema == "fca-thesis-discovery-report-v1"
    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    assert replay.events[-1].event_type is ExecutionEventType.WORKFLOW_COMPLETED
    assert kit.journal is not None
    entry = kit.journal.get(f"workflow-{request.execution_id}")
    assert entry is not None
    assert entry.artifacts[0].uri == f"reports/{request.execution_id}/discovery.json"
    assert entry.artifacts[0].sha256 == report.content_sha256
    kit.journal.verify_integrity()


def test_triage_precedes_every_backtest_cheap_to_expensive(tmp_path: Path) -> None:
    kit = _workflow(
        tmp_path,
        payloads=[
            {
                "candidates": [
                    _candidate("c-expensive", cost=3),
                    _candidate("c-cheap", cost=1),
                ]
            }
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.report is not None
    stages = [(item.candidate.candidate_id, item.stage) for item in kit.triage.requests]
    assert stages == [
        ("c-cheap", "sanity"),
        ("c-cheap", "placebo"),
        ("c-cheap", "plausibility"),
        ("c-expensive", "sanity"),
        ("c-expensive", "placebo"),
        ("c-expensive", "plausibility"),
    ]
    assert [item.bundle.strategy_id for item in kit.runner.requests] == [
        "c-cheap",
        "c-expensive",
    ]
    seed = kit.policy.candidate_seed("c-cheap")
    configuration = cast(
        "Mapping[str, object]",
        kit.runner.requests[0].bundle.harness_configuration,
    )
    assert configuration["seed"] == seed
    attempts = [record for record in kit.ledger.records()]
    ranked = [record for record in attempts if record.kind is AttemptKind.RANKED]
    assert [record.candidate_id for record in ranked] == ["c-cheap", "c-expensive"]
    assert all(record.rationale.statement for record in attempts)


def test_out_of_scope_duplicate_and_budget_refusals_are_recorded(
    tmp_path: Path,
) -> None:
    kit = _workflow(
        tmp_path,
        budget=DiscoveryBudget(
            max_candidates=1, max_model_invocations=2, max_backtests=2
        ),
        payloads=[
            {
                "candidates": [
                    _candidate("c-in", cost=1),
                    _candidate("c-in", cost=2),
                    _candidate("c-out", market="crypto-perp"),
                    _candidate("c-extra", cost=1),
                ]
            }
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.report is not None
    assert outcome.report.outcome is DiscoveryOutcomeKind.IMPLEMENTED
    assert outcome.report.counts == {
        "generated": 1,
        "ranked": 1,
        "rejected": 3,
        "aborted": 0,
        "implemented": 1,
    }
    rejections = [
        record for record in kit.ledger.records() if record.kind is AttemptKind.REJECTED
    ]
    statements = [record.rationale.statement for record in rejections]
    assert any("duplicate candidate id c-in" in item for item in statements)
    assert any("outside the declared workspace scope" in item for item in statements)
    assert any("candidate budget exhausted" in item for item in statements)
    assert all(
        record.rationale.kind is RationaleKind.REJECTION for record in rejections
    )


def test_triage_failure_rejects_candidate_before_any_backtest(tmp_path: Path) -> None:
    kit = _workflow(
        tmp_path,
        triage=FakeTriage({"placebo": "failed"}),
        payloads=[{"candidates": [_candidate()]}],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.COMPLETED
    assert outcome.report is not None
    assert outcome.report.outcome is DiscoveryOutcomeKind.NEGATIVE
    assert kit.runner.requests == []
    assert [item.stage for item in kit.triage.requests] == ["sanity", "placebo"]
    assert outcome.report.counts["rejected"] == 1
    assert outcome.report.counts["implemented"] == 0
    assert "c-momentum [rejected]: candidate failed the placebo check" in (
        outcome.report.negative_findings
    )
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED


def test_inconclusive_plausibility_stops_with_unresolved_uncertainty(
    tmp_path,
) -> None:
    kit = _workflow(
        tmp_path,
        triage=FakeTriage({"plausibility": "inconclusive"}),
        payloads=[{"candidates": [_candidate()]}],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.STOPPED
    assert outcome.fenced is False
    assert outcome.report is not None
    assert outcome.report.outcome is DiscoveryOutcomeKind.UNRESOLVED_UNCERTAINTY
    assert kit.runner.requests == []
    aborted = [
        record for record in kit.ledger.records() if record.kind is AttemptKind.ABORTED
    ]
    assert len(aborted) == 1
    assert "unresolved uncertainty" in aborted[0].rationale.statement
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.STOPPED
    assert kit.journal is not None
    assert kit.journal.get(f"workflow-{request.execution_id}") is not None
    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    assert replay.events[-1].event_type is ExecutionEventType.WORKFLOW_STOPPED


def test_backtest_budget_exhaustion_stops_with_pending_candidates(
    tmp_path: Path,
) -> None:
    kit = _workflow(
        tmp_path,
        budget=DiscoveryBudget(
            max_candidates=4, max_model_invocations=4, max_backtests=1
        ),
        payloads=[
            {
                "candidates": [
                    _candidate("c-cheap", cost=1),
                    _candidate("c-expensive", cost=3),
                ]
            }
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.STOPPED
    assert outcome.report is not None
    assert outcome.report.outcome is DiscoveryOutcomeKind.BUDGET_EXHAUSTED
    assert outcome.report.usage.backtests == 1
    assert outcome.report.counts["implemented"] == 1
    assert list(outcome.report.pending_candidate_ids) == ["c-expensive"]
    assert any(
        "remain unevaluated: c-expensive" in item
        for item in (outcome.report.negative_findings)
    )
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.STOPPED
    assert kit.journal is not None
    assert kit.journal.get(f"workflow-{request.execution_id}") is not None


def test_failed_backtest_aborts_candidate_and_discovery_continues(
    tmp_path: Path,
) -> None:
    kit = _workflow(
        tmp_path,
        runner=FakeRunner(states=["aborted"]),
        payloads=[
            {
                "candidates": [
                    _candidate("c-first", cost=1),
                    _candidate("c-second", cost=2),
                ]
            }
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.COMPLETED
    assert outcome.report is not None
    assert outcome.report.outcome is DiscoveryOutcomeKind.IMPLEMENTED
    assert outcome.report.counts == {
        "generated": 2,
        "ranked": 2,
        "rejected": 0,
        "aborted": 1,
        "implemented": 1,
    }
    assert [item["candidateId"] for item in outcome.report.implemented] == ["c-second"]
    aborted = [
        record for record in kit.ledger.records() if record.kind is AttemptKind.ABORTED
    ]
    assert "ended in state 'aborted'" in aborted[0].rationale.statement
    assert len(kit.runner.requests) == 2


def test_candidate_without_implementation_is_aborted_without_backtest(
    tmp_path,
) -> None:
    kit = _workflow(
        tmp_path,
        payloads=[{"candidates": [_candidate("c-no-code", files=False)]}],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.COMPLETED
    assert outcome.report is not None
    assert outcome.report.outcome is DiscoveryOutcomeKind.NEGATIVE
    assert kit.runner.requests == []
    assert outcome.report.usage.backtests == 0
    assert outcome.report.counts["aborted"] == 1
    aborted = [
        record for record in kit.ledger.records() if record.kind is AttemptKind.ABORTED
    ]
    assert "did not provide an implementation" in aborted[0].rationale.statement


def test_ranking_is_deterministic_cheap_to_expensive(tmp_path: Path) -> None:
    kit = _workflow(
        tmp_path,
        budget=DiscoveryBudget(
            max_candidates=4, max_model_invocations=4, max_backtests=0
        ),
        payloads=[
            {
                "candidates": [
                    _candidate("c-alpha", cost=3, files=False),
                    _candidate("c-beta", cost=1, files=False),
                    _candidate("c-gamma", cost=2, files=False),
                ]
            }
        ],
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.report is not None
    ranked = [
        record for record in kit.ledger.records() if record.kind is AttemptKind.RANKED
    ]
    assert [record.candidate_id for record in ranked] == [
        "c-beta",
        "c-gamma",
        "c-alpha",
    ]
    assert outcome.report.counts == {
        "generated": 3,
        "ranked": 3,
        "rejected": 0,
        "aborted": 3,
        "implemented": 0,
    }
    assert outcome.report.outcome is DiscoveryOutcomeKind.NEGATIVE


def test_steering_fences_generation_and_resume_recomputes(tmp_path: Path) -> None:
    kit = _workflow(
        tmp_path,
        payloads=[{"candidates": [_candidate()]}, {"candidates": [_candidate()]}],
    )
    chat = kit.chat
    chat_id = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=OWNER).chat_id
    request = kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        objective="Discover momentum strategies",
        markets=("equities-us",),
        horizons=("daily",),
        directions=("momentum",),
    )
    kit.provider.steer = lambda: chat.steer_workflow(
        WORKSPACE, request.execution_id, actor_id=OWNER, body="prefer mean reversion"
    )

    outcome = kit.workflow.run(request)

    assert outcome.fenced is True
    assert outcome.phase is DiscoveryPhase.PAUSED
    assert outcome.detail == "steering advanced the intent epoch"
    assert outcome.report is None
    assert outcome.journal_id is None
    assert kit.runner.requests == []
    execution = chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.RUNNING
    assert execution.intent_epoch == 1

    continuation = replace(request, intent_epoch=execution.intent_epoch)
    resumed = kit.workflow.run(continuation)

    assert resumed.fenced is False
    assert resumed.phase is DiscoveryPhase.COMPLETED
    assert len(kit.runner.requests) == 1
    assert len(kit.provider.prompts) >= 2


def test_run_requires_a_live_execution(tmp_path: Path) -> None:
    kit = _workflow(tmp_path, with_journal=False)
    request = DiscoveryRequest(
        workspace_id=WORKSPACE,
        chat_id="chat-1",
        execution_id="execution-x",
        trigger_message_id="message-x",
        requested_by=OWNER,
        objective="Discover momentum strategies",
        markets=("equities-us",),
        horizons=("daily",),
        directions=("momentum",),
    )

    outcome = kit.workflow.run(request)

    assert outcome.phase is DiscoveryPhase.FAILED
    assert outcome.detail is not None
    assert "does not exist" in outcome.detail


def test_report_carries_discovery_limitations_and_deterministic_bytes(
    tmp_path,
) -> None:
    kit = _workflow(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    report = outcome.report
    assert report is not None
    assert DISCOVERY_DISCLOSURE in report.limitations
    assert MULTIPLE_TESTING_DISCLOSURE in report.limitations
    assert report.to_bytes() == report.to_bytes()
    document = report.as_dict()
    assert document["schema"] == "fca-thesis-discovery-report-v1"
    assert document["outcome"] == "implemented"
    assert document["scope"] == {
        "directions": ["mean-reversion", "momentum"],
        "horizons": ["daily"],
        "markets": ["equities-us"],
    }


def test_phase_transitions_are_explicit_and_auditable() -> None:
    assert (
        transition_phase(DiscoveryPhase.REQUESTED, DiscoveryPhase.SCOPING)
        is DiscoveryPhase.SCOPING
    )
    assert (
        transition_phase(DiscoveryPhase.RECORDING, DiscoveryPhase.STOPPED)
        is DiscoveryPhase.STOPPED
    )
    assert (
        transition_phase(DiscoveryPhase.BACKTESTING, DiscoveryPhase.TRIAGING)
        is DiscoveryPhase.TRIAGING
    )
    with pytest.raises(InvalidPhaseTransition, match="requested to recording"):
        transition_phase(DiscoveryPhase.REQUESTED, DiscoveryPhase.RECORDING)
    with pytest.raises(InvalidPhaseTransition, match="stopped to scoping"):
        transition_phase(DiscoveryPhase.STOPPED, DiscoveryPhase.SCOPING)
    assert DISCOVERY_WORKFLOW.phases == DISCOVERY_PHASES


def test_search_policy_budget_guards_are_explicit_decisions() -> None:
    policy = SearchPolicy(
        DiscoveryBudget(max_candidates=2, max_model_invocations=1, max_backtests=1),
        seed=5,
    )

    granted = policy.authorize("backtest", BudgetUsage())
    assert granted.granted is True
    denied = policy.authorize("backtest", BudgetUsage(backtests=1))
    assert denied.granted is False
    assert denied.reason == "backtest budget exhausted: 1 of 1 used"
    invocation_denied = policy.authorize(
        "model_invocation", BudgetUsage(model_invocations=1)
    )
    assert invocation_denied.granted is False
    candidate_denied = policy.authorize(
        "candidate", BudgetUsage(candidates_generated=2)
    )
    assert candidate_denied.granted is False
    assert candidate_denied.reason == ("candidate budget exhausted: 2 of 2 used")
    assert policy.authorize("candidate", BudgetUsage()).granted is True


def test_search_policy_ranking_and_seeding_are_deterministic() -> None:
    policy = SearchPolicy(
        DiscoveryBudget(max_candidates=3, max_model_invocations=1, max_backtests=1),
        seed=5,
    )
    rationale = SearchRationale(kind=RationaleKind.ECONOMIC_PREMISE, statement="drift")

    def candidate(candidate_id: str, cost: int) -> DiscoveryCandidate:
        return DiscoveryCandidate(
            candidate_id=candidate_id,
            hypothesis="hypothesis",
            market="equities-us",
            horizon="daily",
            direction="momentum",
            rationale=rationale,
            cost=cost,
        )

    plan = policy.rank([candidate("c-a", 2), candidate("c-c", 1), candidate("c-b", 1)])
    assert [item.candidate_id for item in plan.ordered] == ["c-b", "c-c", "c-a"]
    assert [item.rank for item in plan.ordered] == [1, 2, 3]
    assert all(item.status.value == "ranked" for item in plan.ordered)
    duplicate_plan = policy.rank([candidate("c-a", 2), candidate("c-a", 2)])
    assert len(duplicate_plan.ordered) == 1
    assert len(duplicate_plan.duplicates) == 1
    with pytest.raises(SearchPolicyError, match="exceeds the discovery budget"):
        policy.rank([candidate(f"c-{index}", 1) for index in range(4)])
    assert policy.candidate_seed("c-a") == policy.candidate_seed("c-a")
    assert policy.candidate_seed("c-a") != policy.candidate_seed("c-b")
    with pytest.raises(SearchPolicyError):
        DiscoveryBudget(max_candidates=0, max_model_invocations=1, max_backtests=1)
    with pytest.raises(SearchPolicyError):
        DiscoveryBudget(max_candidates=1, max_model_invocations=1, max_backtests=-1)


def test_discovery_budget_usage_accounting_is_immutable() -> None:
    usage = BudgetUsage()
    consumed = usage.consumed("candidate").consumed("backtest")
    assert usage.candidates_generated == 0
    assert usage.backtests == 0
    assert consumed.candidates_generated == 1
    assert consumed.backtests == 1
    with pytest.raises(SearchPolicyError):
        BudgetUsage(backtests=-1)
