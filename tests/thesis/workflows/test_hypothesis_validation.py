from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import cast

import pytest

from thesis.agent.context import PromptContext
from thesis.agent.router import (
    InvocationResult,
    ModelProfile,
    ModelResponse,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRegistry, ThesisSkill
from thesis.chat.contracts import ToolJobState, WorkflowExecutionState
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.persistence import CommandJournal
from thesis.workflows.backtests import BacktestRunRequest, BacktestRunResult
from thesis.workflows.evidence import EXPLORATORY_DISCLOSURE
from thesis.workflows.hypothesis_validation import (
    CLAIM_TYPES,
    DATA_READINESS_CHECKS,
    HYPOTHESIS_VALIDATION_PHASES,
    HYPOTHESIS_VALIDATION_WORKFLOW,
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    Criterion,
    DataWindow,
    DataWindowError,
    HypothesisIntakeError,
    HypothesisValidationPhase,
    HypothesisValidationRequest,
    HypothesisValidationWorkflow,
    InvalidPhaseTransition,
    JsonHypothesisComposer,
    StaticReadinessProbe,
    WorkflowError,
    evaluate_hypothesis,
    transition_phase,
)

WORKSPACE = "workspace-demo"
OWNER = "owner-1"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CLAIM = (
    "Five-day momentum in SPY predicts positive five-day forward returns; "
    "a momentum probe strategy should realize a Sharpe of at least 1.0."
)


def _clock() -> Callable[[], datetime]:
    ticks = count(1)

    def clock() -> datetime:
        return NOW.replace(microsecond=next(ticks))

    return clock


def _id_factory() -> Callable[[str], str]:
    counter = count(1)
    return lambda prefix: f"{prefix}-{next(counter)}"


def _envelope() -> dict[str, object]:
    return {
        "claimType": "predictive",
        "claim": CLAIM,
        "variables": ["momentum_5d", "forward_return_5d"],
        "nullHypothesis": (
            "Five-day momentum has no effect on five-day forward returns."
        ),
        "alternativeHypothesis": (
            "Five-day momentum increases five-day forward returns."
        ),
        "estimand": ("mean five-day forward return conditional on five-day momentum"),
        "endpoints": ["forward_return_5d at t+5 close"],
        "dataRequirements": {"symbols": ["SPY"], "minObservations": 60},
        "criteria": [
            {"metric": "sharpe", "operator": "gte", "threshold": 1.0},
            {"metric": "hitRate", "operator": "gte", "threshold": 0.55},
        ],
        "strategy": {
            "strategyId": "momentum-probe",
            "version": "1.0.0",
            "files": {
                "strategy.py": (
                    "from harness.strategy import Strategy\n\n"
                    "class MomentumProbe(Strategy):\n    pass\n"
                )
            },
            "tests": {"test_strategy.py": "def test_strategy():\n    assert True\n"},
            "dependencies": ["numpy==2.1.0"],
            "harnessConfiguration": {"seed": 7, "symbols": ["SPY"]},
            "limitations": ["single-symbol probe"],
        },
        "limitations": ["single-symbol in-sample history only"],
    }


class FakeProvider:
    """Deterministic fake model provider behind the policy boundary."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
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
        return ModelResponse(output=json.dumps(self.payload))


class FakeRunner:
    """Deterministic fake backtest runner behind the runner port."""

    def __init__(
        self,
        *,
        metrics: object | None,
        state: str = "completed",
        on_run: Callable[[], object] | None = None,
    ) -> None:
        self.requests: list[BacktestRunRequest] = []
        self.on_run: Callable[[], object] | None = on_run
        self._metrics = metrics
        self._state = state

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        if self.on_run is not None:
            on_run = self.on_run
            self.on_run = None
            on_run()
        return BacktestRunResult(
            job_id="job-1",
            state=self._state,
            mode="local",
            manifest={"manifestId": "manifest-h-1", "sha256": "c" * 64},
            metrics=self._metrics,
            result_artifacts=[
                {
                    "uri": "run-1/fills.csv",
                    "sha256": "d" * 64,
                    "mediaType": "text/csv",
                }
            ],
        )


class _Kit:
    def __init__(
        self,
        *,
        workflow: HypothesisValidationWorkflow,
        chat: ChatService,
        journal: CommandJournal,
        provider: FakeProvider,
        runner: FakeRunner,
    ) -> None:
        self.workflow = workflow
        self.chat = chat
        self.journal = journal
        self.provider = provider
        self.runner = runner


def _kit(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    runner: FakeRunner | None = None,
    probe: StaticReadinessProbe | None = None,
    state: str = "completed",
    metrics: object | None = None,
    clock: Callable[[], datetime] | None = None,
) -> _Kit:
    selected_clock = clock if clock is not None else _clock()
    id_factory = _id_factory()
    chat = ChatService(clock=selected_clock, id_factory=id_factory)
    selected_provider = provider if provider is not None else FakeProvider(_envelope())
    skills = SkillRegistry(
        (
            ThesisSkill(
                "research",
                "1.0.0",
                "Turn claims into falsifiable, point-in-time analyses.",
                ("read_workspace", "run_backtest"),
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
            allowed_tools=("read_workspace", "run_backtest"),
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
        clock=selected_clock,
        id_factory=lambda: id_factory("invocation"),
    )
    selected_runner = (
        runner
        if runner is not None
        else FakeRunner(
            metrics=metrics
            if metrics is not None
            else {"sharpe": 1.4, "hitRate": 0.58},
            state=state,
        )
    )
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    workflow = HypothesisValidationWorkflow(
        chat=chat,
        router=router,
        runner=selected_runner,
        probe=probe,
        journal=journal,
        clock=selected_clock,
        id_factory=id_factory,
        experiment_id="experiment-1",
    )
    return _Kit(
        workflow=workflow,
        chat=chat,
        journal=journal,
        provider=selected_provider,
        runner=selected_runner,
    )


def _request(kit: _Kit) -> HypothesisValidationRequest:
    chat = kit.chat
    chat_id = chat.create_chat(
        WORKSPACE, title="hypothesis chat", owner_actor_id=OWNER
    ).chat_id
    return kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        claim=CLAIM,
    )


def test_chat_request_executes_documented_path_end_to_end(tmp_path) -> None:
    kit = _kit(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisValidationPhase.COMPLETED
    assert outcome.fenced is False
    assert [item.to_phase for item in outcome.transitions] == [
        HypothesisValidationPhase.SPECIFYING,
        HypothesisValidationPhase.SELECTING,
        HypothesisValidationPhase.BACKTESTING,
        HypothesisValidationPhase.RECORDING,
        HypothesisValidationPhase.COMPLETED,
    ]
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    assert execution.finished_at is not None
    assert execution.tool_job_ids
    specification = outcome.specification
    assert specification is not None
    assert specification.claim_type == "predictive"
    assert specification.claim == CLAIM
    assert specification.variables == ("momentum_5d", "forward_return_5d")
    assert specification.symbols == ("SPY",)
    assert specification.criteria[0].metric == "sharpe"
    assert outcome.backtest is not None
    assert outcome.backtest.succeeded
    assert outcome.journal_id == f"workflow-{request.execution_id}"
    verdict = outcome.verdict
    assert verdict is not None
    assert verdict.outcome == VERDICT_SUPPORTED
    assert verdict.reason == "all pre-registered criteria passed"
    report = outcome.report
    assert report is not None
    assert report.execution_id == request.execution_id


def test_window_is_point_in_time_and_deterministic(tmp_path) -> None:
    kit = _kit(tmp_path, clock=lambda: NOW)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    window = outcome.window
    assert window is not None
    assert window.as_of == NOW
    assert window.end == NOW
    assert window.start == NOW - timedelta(days=30)
    assert window.symbols == ("SPY",)
    readiness = outcome.readiness
    assert readiness is not None
    assert readiness.passed is True
    assert set(readiness.checks) == set(DATA_READINESS_CHECKS)
    assert outcome.backtest is not None
    specification = outcome.specification
    assert specification is not None
    runner_request = kit.runner.requests[0]
    assert runner_request.bundle == specification.bundle
    assert runner_request.workflow_execution_id == request.execution_id
    assert runner_request.experiment_id == "experiment-1"


def test_window_rejects_lookahead_hazard() -> None:
    with pytest.raises(DataWindowError, match="lookahead hazard"):
        DataWindow(
            symbols=["SPY"],
            start=NOW,
            end=NOW + timedelta(days=1),
            as_of=NOW,
        )


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"sharpe": 1.4, "hitRate": 0.58}, VERDICT_SUPPORTED),
        ({"sharpe": 0.2, "hitRate": 0.41}, VERDICT_REFUTED),
        ({"sharpe": 1.4, "hitRate": 0.41}, VERDICT_INCONCLUSIVE),
        ({"sharpe": 1.4}, VERDICT_INCONCLUSIVE),
        (None, VERDICT_INCONCLUSIVE),
    ],
)
def test_criteria_evaluation_outcomes(tmp_path: Path, metrics, expected) -> None:
    kit = _kit(tmp_path, runner=FakeRunner(metrics=metrics))
    request = _request(kit)

    outcome = kit.workflow.run(request)

    verdict = outcome.verdict
    assert verdict is not None
    assert verdict.outcome == expected
    report = outcome.report
    assert report is not None
    assert report.verdict_outcome == expected
    if expected == VERDICT_INCONCLUSIVE:
        assert verdict.insufficient is True
        assert verdict.reason != ""
    else:
        assert len(verdict.evaluations) == 2


def test_readiness_gate_fails_workflow_before_backtest(tmp_path) -> None:
    kit = _kit(
        tmp_path,
        probe=StaticReadinessProbe({"coverage": False, "leakage": False}),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisValidationPhase.FAILED
    assert outcome.fenced is False
    assert outcome.detail is not None
    assert "coverage" in outcome.detail
    assert "leakage" in outcome.detail
    assert kit.runner.requests == []
    assert outcome.verdict is None
    assert outcome.report is None
    assert kit.journal.entries() == ()
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.FAILED
    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    assert replay.events[-1].event_type is ExecutionEventType.WORKFLOW_FAILED


def test_steering_is_non_destructive_and_fences_stale_continuations(
    tmp_path,
) -> None:
    kit = _kit(tmp_path)
    chat = kit.chat
    chat_id = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=OWNER).chat_id
    request = kit.workflow.launch(WORKSPACE, chat_id, actor_id=OWNER, claim=CLAIM)
    kit.provider.steer = lambda: chat.steer_workflow(
        WORKSPACE, request.execution_id, actor_id=OWNER, body="use a longer window"
    )

    outcome = kit.workflow.run(request)

    assert outcome.fenced is True
    assert outcome.phase is HypothesisValidationPhase.PAUSED
    assert outcome.detail == "steering advanced the intent epoch"
    assert outcome.report is None
    assert outcome.journal_id is None
    assert kit.runner.requests == []
    execution = chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.RUNNING
    assert execution.intent_epoch == 1
    assert len(execution.steering_message_ids) == 1

    continuation = replace(request, intent_epoch=execution.intent_epoch)
    resumed = kit.workflow.run(continuation)

    assert resumed.fenced is False
    assert resumed.phase is HypothesisValidationPhase.COMPLETED
    assert len(kit.provider.prompts) == 2


def test_in_flight_backtest_result_is_kept_and_stale_continuation_fenced(
    tmp_path,
) -> None:
    kit = _kit(tmp_path)
    chat = kit.chat
    chat_id = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=OWNER).chat_id
    request = kit.workflow.launch(WORKSPACE, chat_id, actor_id=OWNER, claim=CLAIM)
    kit.runner.on_run = lambda: chat.steer_workflow(
        WORKSPACE, request.execution_id, actor_id=OWNER, body="narrow the claim"
    )

    outcome = kit.workflow.run(request)

    assert outcome.fenced is True
    assert outcome.phase is HypothesisValidationPhase.PAUSED
    assert outcome.backtest is not None
    assert outcome.journal_id is None
    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    submitted = [
        event
        for event in replay.events
        if event.event_type is ExecutionEventType.TOOL_SUBMITTED
    ]
    job = chat.get_tool_job(WORKSPACE, str(submitted[0].payload["toolJobId"]))
    assert job.state is ToolJobState.SUCCEEDED
    assert job.late_result is True
    execution = chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.RUNNING
    assert execution.intent_epoch == 1

    continuation = replace(request, intent_epoch=execution.intent_epoch)
    resumed = kit.workflow.run(continuation)

    assert resumed.fenced is False
    assert resumed.phase is HypothesisValidationPhase.COMPLETED
    assert kit.journal.get(f"workflow-{request.execution_id}") is not None


def test_reconnecting_client_observes_progress_and_final_verdict(tmp_path) -> None:
    kit = _kit(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    types = [event.event_type for event in replay.events]
    assert types[0] is ExecutionEventType.MESSAGE_ACCEPTED
    assert types[1] is ExecutionEventType.WORKFLOW_LAUNCHED
    assert ExecutionEventType.WORKFLOW_STARTED in types
    assert types.count(ExecutionEventType.TOOL_SUBMITTED) == 1
    assert types.count(ExecutionEventType.TOOL_STARTED) == 1
    assert types.count(ExecutionEventType.TOOL_RESULT) == 1
    assert types[-1] is ExecutionEventType.WORKFLOW_COMPLETED
    final = replay.events[-1]
    report_reference = cast("dict[str, object]", final.payload["report"])
    assert outcome.report is not None
    assert report_reference["sha256"] == outcome.report.content_sha256
    assert report_reference["schema"] == "fca-thesis-hypothesis-verdict-v1"
    page = kit.workflow.observe(
        WORKSPACE, request.execution_id, after_cursor=3, limit=2
    )
    assert [event.cursor for event in page.events] == [4, 5]
    assert page.has_more
    tail = kit.workflow.observe(
        WORKSPACE, request.execution_id, after_cursor=page.cursor
    )
    assert tail.events[-1].event_type is ExecutionEventType.WORKFLOW_COMPLETED


def test_report_links_claim_to_reproducible_artifacts(tmp_path) -> None:
    kit = _kit(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    report = outcome.report
    specification = outcome.specification
    assert report is not None and specification is not None
    assert report.schema == "fca-thesis-hypothesis-verdict-v1"
    assert report.strategy_id == specification.bundle.strategy_id
    assert report.strategy_version == specification.bundle.version
    assert report.bundle_sha256 == specification.bundle.bundle_sha256
    assert report.backtest_job_id == "job-1"
    assert report.harness_manifest_reference is not None
    assert report.harness_manifest_reference.startswith("manifest-h-1@sha256:")
    assert report.criteria == specification.criteria
    assert report.metrics == {"sharpe": 1.4, "hitRate": 0.58}
    artifacts = cast("list[dict[str, object]]", report.as_dict()["artifactReferences"])
    assert artifacts
    assert report.to_bytes() == report.to_bytes()
    document = report.as_dict()
    window_document = cast("dict[str, object]", document["window"])
    readiness_document = cast("dict[str, object]", document["readiness"])
    assert window_document["asOf"] == report.window["asOf"]
    assert readiness_document["passed"] is True
    entry = kit.journal.get(f"workflow-{request.execution_id}")
    assert entry is not None
    assert entry.artifacts[0].uri == (
        f"reports/{request.execution_id}/hypothesis-verdict.json"
    )
    assert entry.artifacts[0].sha256 == report.content_sha256
    kit.journal.verify_integrity()


def test_findings_are_labeled_exploratory_with_inferential_fields(tmp_path) -> None:
    kit = _kit(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    report = outcome.report
    assert report is not None
    assert report.evidence_grade == "exploratory"
    assert report.historical is True
    assert report.phase_label == "phase-1-historical"
    assert EXPLORATORY_DISCLOSURE in report.limitations
    assert "single-symbol in-sample history only" in report.limitations
    document = report.as_dict()
    assert document["evidenceGrade"] == "exploratory"
    assert document["claimType"] == "predictive"
    assert document["nullHypothesis"] != ""
    assert document["alternativeHypothesis"] != ""
    assert document["estimand"] != ""
    assert document["endpoints"]
    assert document["variables"]
    with pytest.raises(WorkflowError, match="grade"):
        replace(report, evidence_grade="validated")


def test_claim_types_are_distinguished_at_intake(tmp_path) -> None:
    envelope = _envelope()
    envelope["claimType"] = "causal"
    envelope["estimand"] = "average treatment effect of momentum on returns"
    kit = _kit(tmp_path, provider=FakeProvider(envelope))
    request = _request(kit)

    outcome = kit.workflow.run(request)

    specification = outcome.specification
    assert specification is not None
    assert specification.claim_type == "causal"
    assert specification.claim_type in CLAIM_TYPES
    report = outcome.report
    assert report is not None
    assert report.claim_type == "causal"


def test_failed_backtest_state_fails_workflow_with_recorded_result(tmp_path) -> None:
    kit = _kit(tmp_path, state="aborted")
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisValidationPhase.FAILED
    assert outcome.backtest is not None
    assert outcome.report is None
    assert kit.journal.entries() == ()
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.FAILED


def test_run_requires_a_live_execution(tmp_path) -> None:
    kit = _kit(tmp_path)
    request = HypothesisValidationRequest(
        workspace_id=WORKSPACE,
        chat_id="chat-1",
        execution_id="execution-x",
        trigger_message_id="message-x",
        requested_by=OWNER,
        claim=CLAIM,
    )

    outcome = kit.workflow.run(request)

    assert outcome.phase is HypothesisValidationPhase.FAILED
    assert outcome.detail is not None
    assert "does not exist" in outcome.detail


def test_phase_transitions_are_explicit_and_auditable() -> None:
    assert (
        transition_phase(
            HypothesisValidationPhase.REQUESTED,
            HypothesisValidationPhase.SPECIFYING,
        )
        is HypothesisValidationPhase.SPECIFYING
    )
    with pytest.raises(InvalidPhaseTransition, match="requested to recording"):
        transition_phase(
            HypothesisValidationPhase.REQUESTED,
            HypothesisValidationPhase.RECORDING,
        )
    with pytest.raises(InvalidPhaseTransition, match="completed to specifying"):
        transition_phase(
            HypothesisValidationPhase.COMPLETED,
            HypothesisValidationPhase.SPECIFYING,
        )
    assert HYPOTHESIS_VALIDATION_WORKFLOW.phases == HYPOTHESIS_VALIDATION_PHASES


def test_evaluate_hypothesis_is_conservative_without_evidence() -> None:
    criteria = (
        Criterion(metric="sharpe", operator="gte", threshold=1.0),
        Criterion(metric="hitRate", operator="gte", threshold=0.55),
    )
    with pytest.raises(WorkflowError, match="pre-registered criteria"):
        evaluate_hypothesis((), {"sharpe": 1.0})
    missing = evaluate_hypothesis(criteria, None)
    assert missing.outcome == VERDICT_INCONCLUSIVE
    assert missing.insufficient is True
    assert missing.reason == "no backtest metrics were recorded"
    non_numeric = evaluate_hypothesis(criteria, {"sharpe": True, "hitRate": 0.6})
    assert non_numeric.outcome == VERDICT_INCONCLUSIVE
    assert "sharpe" in non_numeric.reason
    supported = evaluate_hypothesis(criteria, {"sharpe": 1.0, "hitRate": 0.55})
    assert supported.outcome == VERDICT_SUPPORTED
    refuted = evaluate_hypothesis(criteria, {"sharpe": 0.9, "hitRate": 0.5})
    assert refuted.outcome == VERDICT_REFUTED


@pytest.mark.parametrize(
    "mutation",
    [
        {"claimType": "validated"},
        {"claim": ""},
        {"variables": []},
        {"endpoints": []},
        {"nullHypothesis": ""},
        {"estimand": ""},
        {"criteria": []},
        {"criteria": [{"metric": "sharpe", "operator": "between", "threshold": 1}]},
        {"dataRequirements": {"symbols": []}},
        {"strategy": {"strategyId": "probe", "version": "1.0.0"}},
        {"strategy": None},
    ],
)
def test_composer_rejects_invalid_envelopes(mutation) -> None:
    payload = _envelope()
    for key, value in mutation.items():
        payload[key] = value

    with pytest.raises(HypothesisIntakeError):
        JsonHypothesisComposer().compose(_invocation(payload))


def test_composer_rejects_non_json_output() -> None:
    class _Response:
        output = "not json"

    class _Provenance:
        def as_dict(self) -> dict[str, object]:
            return {}

    class _Invocation:
        response = _Response()
        provenance = _Provenance()

    with pytest.raises(HypothesisIntakeError):
        JsonHypothesisComposer().compose(cast("InvocationResult", _Invocation()))


def test_composer_rejects_empty_output() -> None:
    class _Response:
        output = "   "

    class _Provenance:
        def as_dict(self) -> dict[str, object]:
            return {}

    class _Invocation:
        response = _Response()
        provenance = _Provenance()

    with pytest.raises(HypothesisIntakeError, match="no inline claim"):
        JsonHypothesisComposer().compose(cast("InvocationResult", _Invocation()))


def _invocation(payload: dict[str, object]) -> InvocationResult:
    class _Provenance:
        def as_dict(self) -> dict[str, object]:
            return {}

    class _Invocation:
        response = ModelResponse(output=json.dumps(payload))
        provenance = _Provenance()

    return cast("InvocationResult", _Invocation())
