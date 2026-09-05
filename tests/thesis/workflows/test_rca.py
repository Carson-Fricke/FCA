from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import cast

import pytest

from thesis.chat.contracts import WorkflowExecutionState
from thesis.chat.service import ChatService
from thesis.experiments import (
    ClaimKind,
    ExperimentContract,
    Partition,
    PartitionRole,
    StoppingRule,
    StoppingRuleKind,
)
from thesis.persistence import CommandJournal
from thesis.research.lineage import (
    LineageError,
    RevisionLineage,
    RevisionNode,
    RevisionOutcome,
    RunIdentity,
)
from thesis.strategies.bundles import StrategyBundle
from thesis.workflows.backtests import BacktestRunRequest, BacktestRunResult
from thesis.workflows.evidence import EXPLORATORY_DISCLOSURE
from thesis.workflows.rca import (
    DEFAULT_MAX_ITERATIONS,
    FAILURE_CLASSES,
    RCA_LOOP_PHASES,
    RCA_REVISION_LOOP_WORKFLOW,
    REVISABLE_CLASSES,
    AlternativeExplanation,
    ClassificationError,
    FailureClass,
    InvalidPhaseTransition,
    MetricRule,
    RcaFinding,
    RcaIntakeError,
    RcaLoopDisposition,
    RcaLoopPhase,
    RcaRevisionLoopOutcome,
    RcaRevisionLoopRequest,
    RcaRevisionLoopWorkflow,
    RevisionProposal,
    RevisionProposalError,
    WorkflowError,
    build_rca_loop_report,
    classify_run,
    collect_signals,
    transition_phase,
)

WORKSPACE = "workspace-demo"
OWNER = "owner-1"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
OBJECTIVE = "diagnose the momentum probe and revise it within budget"
CLAIM = "claim-1"
REVISED_CLAIM = "claim-2"
GOOD_METRICS = {"sharpe": 1.4, "hitRate": 0.58}
BAD_METRICS = {"sharpe": 0.2, "hitRate": 0.41}
RULES = (
    MetricRule("sharpe", "gte", 1.0),
    MetricRule("hitRate", "gte", 0.55),
)
MANIFEST = {"manifestId": "manifest-h-1", "sha256": "c" * 64}


def _clock() -> Callable[[], datetime]:
    ticks = count(1)

    def clock() -> datetime:
        return NOW.replace(microsecond=next(ticks))

    return clock


def _id_factory() -> Callable[[str], str]:
    counter = count(1)
    return lambda prefix: f"{prefix}-{next(counter)}"


def _bundle(version: str = "1.0.0") -> StrategyBundle:
    return StrategyBundle.create(
        strategy_id="momentum-probe",
        version=version,
        source={
            "strategy.py": (
                "from harness.strategy import Strategy\n\n"
                "class MomentumProbe(Strategy):\n    pass\n"
            )
        },
        tests={"test_strategy.py": "def test_strategy():\n    assert True\n"},
        dependencies=["numpy==2.1.0"],
        harness_configuration={"seed": 7, "symbols": ["SPY"]},
        limitations=["single-symbol probe"],
    )


def _contract(version: str = "1.0.0") -> ExperimentContract:
    return ExperimentContract(
        workspace_id=WORKSPACE,
        experiment_id="experiment-1",
        version=version,
        title="Momentum probe validation",
        claim_kind=ClaimKind.DESCRIPTIVE,
        hypothesis="five-day momentum realizes a positive Sharpe",
        primary_endpoint="sharpe",
        stopping_rule=StoppingRule(
            kind=StoppingRuleKind.FIXED_SAMPLE,
            description="one look after the window closes",
        ),
        partitions=(Partition(role=PartitionRole.TRAIN, start_ns=0, end_ns=1_000_000),),
        created_by=OWNER,
        created_at=NOW.isoformat(),
    )


def _run(
    *,
    metrics: object | None = None,
    state: str = "completed",
    metadata: dict[str, object] | None = None,
    job_id: str = "job-1",
) -> BacktestRunResult:
    return BacktestRunResult(
        job_id=job_id,
        state=state,
        mode="local",
        manifest=MANIFEST,
        metrics=metrics,
        metadata=metadata or {},
    )


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class FakeRunner:
    """Deterministic fake backtest runner behind the runner port."""

    def __init__(
        self,
        results: Sequence[BacktestRunResult] = (),
        *,
        default: BacktestRunResult | None = None,
    ) -> None:
        self.requests: list[BacktestRunRequest] = []
        self._results = list(results)
        self._default = default

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        if self._results:
            return self._results.pop(0)
        if self._default is not None:
            return self._default
        raise AssertionError("unexpected extra backtest run")


class FakeProposer:
    """Deterministic fake revision proposer behind the proposal port."""

    def __init__(
        self,
        proposals: Sequence[RevisionProposal] = (),
        *,
        default: RevisionProposal | None = None,
        steer: Callable[[], object] | None = None,
    ) -> None:
        self.calls: list[tuple[RcaFinding, BacktestRunResult | None, int]] = []
        self._proposals = list(proposals)
        self._default = default
        self.steer: Callable[[], object] | None = steer

    def propose(
        self,
        finding: RcaFinding,
        run: BacktestRunResult | None,
        attempt: int,
    ) -> RevisionProposal:
        self.calls.append((finding, run, attempt))
        if self.steer is not None:
            steer_once = self.steer
            self.steer = None
            steer_once()
        if self._proposals:
            return self._proposals.pop(0)
        if self._default is not None:
            return self._default
        raise RevisionProposalError("no revision available")


def _proposal(
    *,
    version: str = "1.1.0",
    claim: str = REVISED_CLAIM,
    supersedes: str | None = CLAIM,
) -> RevisionProposal:
    return RevisionProposal(
        bundle=_bundle(version),
        contract=_contract(version),
        claim_id=claim,
        rationale="tighten the entry filter and re-test the premise",
        supersedes_claim_id=supersedes,
    )


class _Kit:
    def __init__(
        self,
        *,
        workflow: RcaRevisionLoopWorkflow,
        chat: ChatService,
        journal: CommandJournal,
        runner: FakeRunner,
        proposer: FakeProposer,
        lineage: RevisionLineage,
    ) -> None:
        self.workflow = workflow
        self.chat = chat
        self.journal = journal
        self.runner = runner
        self.proposer = proposer
        self.lineage = lineage


def _kit(
    tmp_path: Path,
    *,
    runner: FakeRunner | None = None,
    proposer: FakeProposer | None = None,
) -> _Kit:
    selected_clock = _clock()
    id_factory = _id_factory()
    chat = ChatService(clock=selected_clock, id_factory=id_factory)
    lineage = RevisionLineage()
    selected_runner = runner if runner is not None else FakeRunner()
    selected_proposer = proposer if proposer is not None else FakeProposer()
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    workflow = RcaRevisionLoopWorkflow(
        chat=chat,
        runner=selected_runner,
        proposer=selected_proposer,
        lineage=lineage,
        journal=journal,
        clock=selected_clock,
        id_factory=id_factory,
    )
    return _Kit(
        workflow=workflow,
        chat=chat,
        journal=journal,
        runner=selected_runner,
        proposer=selected_proposer,
        lineage=lineage,
    )


def _request(
    kit: _Kit,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    initial_run: BacktestRunResult | None = None,
) -> RcaRevisionLoopRequest:
    chat_id = kit.chat.create_chat(
        WORKSPACE, title="rca chat", owner_actor_id=OWNER
    ).chat_id
    execution = kit.workflow.launch(
        WORKSPACE, chat_id, actor_id=OWNER, objective=OBJECTIVE
    )
    return RcaRevisionLoopRequest(
        workspace_id=WORKSPACE,
        chat_id=chat_id,
        execution_id=execution.execution_id,
        trigger_message_id=execution.trigger_message_id,
        requested_by=OWNER,
        objective=OBJECTIVE,
        claim_id=CLAIM,
        initial_bundle=_bundle("1.0.0"),
        initial_contract=_contract("1.0.0"),
        rules=RULES,
        max_iterations=max_iterations,
        initial_run=initial_run,
        intent_epoch=execution.intent_epoch,
        created_at=execution.created_at,
    )


# -- lineage ---------------------------------------------------------------


def _lineage_node(
    node_id: str,
    revision: int,
    parent: RevisionNode | None = None,
    *,
    outcome: RevisionOutcome = RevisionOutcome.FAILED,
    claim: str = CLAIM,
    contract_sha256: str | None = None,
    supersedes: str | None = None,
    run: RunIdentity | None = None,
) -> RevisionNode:
    failed = outcome is RevisionOutcome.FAILED
    if contract_sha256 is None:
        contract_sha256 = _digest(f"contract-{revision}")
    return RevisionNode(
        node_id=node_id,
        revision=revision,
        run=run or RunIdentity(f"job-{revision}"),
        strategy_id="momentum-probe",
        strategy_version=f"1.{revision}.0",
        strategy_lineage_version=f"momentum-probe-v{revision}",
        experiment_id="experiment-1",
        outcome=outcome,
        claim_id=claim,
        contract_sha256=contract_sha256,
        rationale="recorded rationale",
        created_at=NOW,
        parent_node_id=None if parent is None else parent.node_id,
        parent_node_sha256=None if parent is None else parent.content_sha256,
        previous_contract_sha256=(None if parent is None else parent.contract_sha256),
        failure_class="strategy" if failed else None,
        supersedes_claim_id=supersedes,
    )


def test_run_identity_key_prefers_the_manifest() -> None:
    identified = RunIdentity(
        run_id="job-1", manifest_id="manifest-h-1", manifest_sha256="c" * 64
    )
    assert identified.identity_key == f"manifest-h-1@sha256:{'c' * 64}"
    assert RunIdentity(run_id="job-1").identity_key == "run:job-1"


def test_lineage_chain_queries_and_branches() -> None:
    root = _lineage_node("root", 0)
    child = _lineage_node("child", 1, root, claim=REVISED_CLAIM, supersedes=CLAIM)
    lineage = RevisionLineage((root, child))

    assert len(lineage) == 2
    assert "root" in lineage
    assert lineage.chain("child") == (root, child)
    assert lineage.root("child") is root
    assert lineage.ancestors("child") == (root,)
    assert lineage.children("root") == (child,)
    assert lineage.branch_factor("root") == 1
    assert lineage.max_branch_factor == 1
    assert child.contract_transition == (
        f"{root.contract_sha256}..{child.contract_sha256}"
    )


def test_lineage_rejects_a_broken_parent_chain() -> None:
    root = _lineage_node("root", 0)
    orphan = _lineage_node("orphan", 1, root)
    broken = replace(orphan, parent_node_sha256=_digest("not-the-parent"))
    with pytest.raises(LineageError, match="parent digest"):
        RevisionLineage((root, broken))


def test_lineage_requires_a_new_contract_per_revision() -> None:
    root = _lineage_node("root", 0)
    with pytest.raises(LineageError, match="new experiment contract"):
        RevisionLineage(
            (
                root,
                _lineage_node(
                    "child",
                    1,
                    root,
                    contract_sha256=root.contract_sha256,
                ),
            )
        )


def test_lineage_requires_incrementing_revisions() -> None:
    root = _lineage_node("root", 0)
    child = _lineage_node("child", 1, root)
    with pytest.raises(LineageError, match="increment the parent revision"):
        RevisionLineage((root, child, _lineage_node("leaf", 1, child)))


def test_lineage_blocks_rewriting_a_failed_claim() -> None:
    root = _lineage_node("root", 0, outcome=RevisionOutcome.FAILED, claim=CLAIM)
    rewrite = _lineage_node(
        "child", 1, root, outcome=RevisionOutcome.PASSED, claim=CLAIM
    )
    with pytest.raises(LineageError, match="cannot become positive"):
        RevisionLineage((root, rewrite))


def test_lineage_requires_the_superseded_failed_claim() -> None:
    root = _lineage_node("root", 0, outcome=RevisionOutcome.FAILED, claim=CLAIM)
    without_supersede = _lineage_node(
        "child", 1, root, outcome=RevisionOutcome.PASSED, claim=REVISED_CLAIM
    )
    with pytest.raises(LineageError, match="superseded failed claim"):
        RevisionLineage((root, without_supersede))
    superseding = _lineage_node(
        "child",
        1,
        root,
        outcome=RevisionOutcome.PASSED,
        claim=REVISED_CLAIM,
        supersedes=CLAIM,
    )
    lineage = RevisionLineage((root, superseding))
    assert lineage.get("child").supersedes_claim_id == CLAIM


def test_lineage_verify_detects_tampering() -> None:
    root = _lineage_node("root", 0)
    lineage = RevisionLineage((root,))
    tampered = lineage.get("root")
    object.__setattr__(tampered, "rationale", "rewritten history")
    with pytest.raises(LineageError, match="was modified"):
        lineage.verify()


def test_lineage_content_hash_is_deterministic() -> None:
    first = _lineage_node("root", 0)
    second = _lineage_node("root", 0)
    assert first.content_sha256 == second.content_sha256
    assert first.content_sha256 == _digest(
        __import__("json").dumps(first.document, sort_keys=True, separators=(",", ":"))
    )


# -- taxonomy --------------------------------------------------------------


def test_classify_harness_when_no_run_is_recorded() -> None:
    finding = classify_run(None, RULES)

    assert finding.failure_class is FailureClass.HARNESS
    assert finding.rule == "no-run-recorded"
    assert finding.revisable is False


def test_classify_harness_on_a_failed_run_state() -> None:
    finding = classify_run(_run(state="failed"), RULES)

    assert finding.failure_class is FailureClass.HARNESS
    assert finding.rule == "run-not-terminal-success"
    assert "runState" in finding.supporting


def test_classify_data_defects_from_recorded_issues() -> None:
    finding = classify_run(
        _run(metadata={"dataIssues": ["coverage", "leakage"]}), RULES
    )

    assert finding.failure_class is FailureClass.DATA
    assert finding.rule == "data-issues-recorded"
    assert finding.signals.data_issues == ("coverage", "leakage")
    assert not finding.revisable


def test_classify_implementation_defects_from_strategy_errors() -> None:
    finding = classify_run(_run(metadata={"strategyError": "boom"}), RULES)

    assert finding.failure_class is FailureClass.IMPLEMENTATION
    assert finding.rule == "strategy-error-recorded"
    assert finding.revisable is True


def test_classify_harness_defects_from_engine_errors() -> None:
    finding = classify_run(_run(metadata={"harnessError": "worker died"}), RULES)

    assert finding.failure_class is FailureClass.HARNESS
    assert finding.rule == "harness-error-recorded"
    assert not finding.revisable


def test_classify_strategy_failure_without_regime_evidence() -> None:
    finding = classify_run(_run(metrics=BAD_METRICS), RULES)

    assert finding.failure_class is FailureClass.STRATEGY
    assert finding.rule == "rules-failed"
    assert "regimeBreakdown:absent" in finding.disconfirming
    assert any(
        alternative.failure_class is FailureClass.MARKET_REGIME
        for alternative in finding.alternatives
    )
    assert finding.revisable is True


def test_classify_market_regime_with_recorded_breakdown() -> None:
    finding = classify_run(
        _run(metrics=BAD_METRICS, metadata={"regimeBreakdown": {"covid": {}}}),
        RULES,
    )

    assert finding.failure_class is FailureClass.MARKET_REGIME
    assert finding.rule == "regime-breakdown-recorded"
    assert finding.signals.regime_segments == ("covid",)
    assert any(
        alternative.failure_class is FailureClass.STRATEGY
        for alternative in finding.alternatives
    )


def test_classify_passing_runs_record_no_failure() -> None:
    finding = classify_run(_run(metrics=GOOD_METRICS), RULES)

    assert finding.failure_class is None
    assert finding.confidence is None
    assert finding.follow_ups == ()
    assert finding.revisable is False


def test_classify_missing_and_incomplete_metrics_as_harness() -> None:
    missing = classify_run(_run(metrics=None), RULES)
    incomplete = classify_run(_run(metrics={"sharpe": 1.4}), RULES)

    assert missing.failure_class is FailureClass.HARNESS
    assert missing.rule == "metrics-missing"
    assert incomplete.failure_class is FailureClass.HARNESS
    assert incomplete.rule == "metrics-incomplete"
    assert incomplete.signals.evaluations[1].passed is None


def test_classification_records_evidence_and_is_deterministic() -> None:
    run = _run(metrics=BAD_METRICS)

    first = classify_run(run, RULES)
    second = classify_run(run, RULES)

    assert first.as_dict() == second.as_dict()
    assert 0.0 <= cast(float, first.confidence) <= 1.0
    assert first.supporting
    assert first.disconfirming
    assert first.follow_ups
    for alternative in first.alternatives:
        assert alternative.confidence < cast(float, first.confidence)
        assert alternative.failure_class is not first.failure_class
        assert isinstance(alternative, AlternativeExplanation)


def test_collect_signals_requires_pre_registered_rules() -> None:
    with pytest.raises(ClassificationError, match="pre-registered rules"):
        collect_signals(_run(metrics=GOOD_METRICS), ())


# -- bounded revision loop -------------------------------------------------


def test_loop_revises_once_and_records_the_pass(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner((_run(metrics=BAD_METRICS), _run(metrics=GOOD_METRICS))),
        proposer=FakeProposer((_proposal(),)),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is RcaLoopPhase.COMPLETED
    assert outcome.fenced is False
    assert outcome.disposition is RcaLoopDisposition.PASSED
    assert outcome.revisions_used == 1
    assert [cycle.revision for cycle in outcome.cycles] == [0, 1]
    assert [item.to_phase for item in outcome.transitions] == [
        RcaLoopPhase.ANALYZING,
        RcaLoopPhase.CLASSIFYING,
        RcaLoopPhase.RECORDING,
        RcaLoopPhase.REVISING,
        RcaLoopPhase.ANALYZING,
        RcaLoopPhase.CLASSIFYING,
        RcaLoopPhase.RECORDING,
        RcaLoopPhase.COMPLETED,
    ]
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    assert [item.run_id for item in kit.runner.requests] == [
        f"rca-{request.execution_id}-baseline",
        f"rca-{request.execution_id}-rev1",
    ]
    root, child = kit.lineage.nodes
    assert root.outcome is RevisionOutcome.FAILED
    assert root.failure_class == "strategy"
    assert child.outcome is RevisionOutcome.PASSED
    assert child.claim_id == REVISED_CLAIM
    assert child.supersedes_claim_id == CLAIM
    assert child.contract_transition == (
        f"{_contract('1.0.0').contract_sha256}..{_contract('1.1.0').contract_sha256}"
    )
    kit.lineage.verify()
    report = outcome.report
    assert report is not None
    assert report.disposition is RcaLoopDisposition.PASSED
    assert report.max_iterations == DEFAULT_MAX_ITERATIONS
    assert report.iterations_used == 1
    assert report.lineage_size == 2
    assert EXPLORATORY_DISCLOSURE in report.limitations
    assert report.content_sha256 == hashlib.sha256(report.to_bytes()).hexdigest()
    assert outcome.journal_id == f"workflow-{request.execution_id}"


def test_loop_stops_at_the_explicit_revision_budget(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner(default=_run(metrics=BAD_METRICS)),
        proposer=FakeProposer((_proposal(version="1.1.0"), _proposal(version="1.2.0"))),
    )
    request = _request(kit, max_iterations=2)

    outcome = kit.workflow.run(request)

    assert outcome.disposition is RcaLoopDisposition.EXHAUSTED
    assert outcome.revisions_used == 2
    assert len(kit.runner.requests) == 3
    assert len(kit.proposer.calls) == 2
    assert [call[2] for call in kit.proposer.calls] == [1, 2]
    assert len(kit.lineage.nodes) == 3
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    report = outcome.report
    assert report is not None
    assert report.disposition is RcaLoopDisposition.EXHAUSTED
    assert report.max_iterations == 2
    assert report.iterations_used == 2


@pytest.mark.parametrize(
    ("metadata", "failure_class"),
    [
        ({"harnessError": "worker died"}, "harness"),
        ({"dataIssues": ["coverage"]}, "data"),
    ],
)
def test_engine_and_data_defects_block_revision(
    tmp_path: Path, metadata: dict[str, object], failure_class: str
) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner((_run(metadata=metadata),)),
        proposer=FakeProposer((_proposal(),)),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.disposition is RcaLoopDisposition.BLOCKED
    assert outcome.revisions_used == 0
    assert kit.proposer.calls == []
    assert len(kit.runner.requests) == 1
    (node,) = kit.lineage.nodes
    assert node.outcome is RevisionOutcome.FAILED
    assert node.failure_class == failure_class
    report = outcome.report
    assert report is not None
    final = report.final_finding
    assert final.failure_class is not None
    assert final.failure_class.value == failure_class


def test_loop_cannot_rewrite_a_failed_claim_without_a_new_claim(
    tmp_path: Path,
) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner((_run(metrics=BAD_METRICS), _run(metrics=GOOD_METRICS))),
        proposer=FakeProposer(
            (_proposal(claim=CLAIM, supersedes=None),),
        ),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is RcaLoopPhase.FAILED
    assert outcome.detail is not None
    assert "cannot become positive" in outcome.detail
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.FAILED
    assert len(kit.lineage.nodes) == 1


def test_loop_pauses_when_steering_arrives_mid_revision(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner((_run(metrics=BAD_METRICS),)),
        proposer=FakeProposer((_proposal(),)),
    )
    request = _request(kit)

    def steer_once() -> None:
        kit.workflow.steer(
            WORKSPACE, request.execution_id, actor_id=OWNER, body="stop revising"
        )

    kit.proposer.steer = steer_once
    outcome = kit.workflow.run(request)

    assert outcome.phase is RcaLoopPhase.PAUSED
    assert outcome.fenced is True
    assert outcome.disposition is None
    assert len(kit.runner.requests) == 1
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.RUNNING


def test_loop_requests_are_validated_at_intake(tmp_path: Path) -> None:
    kit = _kit(tmp_path)
    request = _request(kit)

    with pytest.raises(RcaIntakeError, match="positive integer"):
        replace(request, max_iterations=0)
    with pytest.raises(RcaIntakeError, match="MetricRule"):
        replace(request, rules=())
    with pytest.raises(RcaIntakeError, match="initial_run"):
        replace(request, initial_run="not-a-run")
    with pytest.raises(RcaIntakeError, match="initial_bundle"):
        replace(request, initial_bundle="not-a-bundle")


def test_transition_table_rejects_invalid_moves() -> None:
    with pytest.raises(InvalidPhaseTransition):
        transition_phase(RcaLoopPhase.REQUESTED, RcaLoopPhase.COMPLETED)
    with pytest.raises(InvalidPhaseTransition):
        transition_phase(RcaLoopPhase.COMPLETED, RcaLoopPhase.ANALYZING)


def test_workflow_metadata_is_versioned_and_ordered() -> None:
    assert RCA_REVISION_LOOP_WORKFLOW.workflow_id == "rca-revision-loop"
    assert RCA_REVISION_LOOP_WORKFLOW.phases == RCA_LOOP_PHASES
    assert set(FAILURE_CLASSES) == set(REVISABLE_CLASSES) | {"data", "harness"}


def test_proposal_errors_fail_the_workflow_cleanly(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner((_run(metrics=BAD_METRICS),)),
        proposer=FakeProposer(),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is RcaLoopPhase.FAILED
    assert outcome.detail is not None
    assert "no revision available" in outcome.detail
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.FAILED


def test_build_report_requires_recorded_phases(tmp_path: Path) -> None:
    kit = _kit(tmp_path)
    request = _request(kit)
    lineage = RevisionLineage()

    with pytest.raises(WorkflowError, match="positive max_iterations"):
        build_rca_loop_report(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            claim_id=request.claim_id,
            objective=request.objective,
            disposition=RcaLoopDisposition.PASSED,
            max_iterations=0,
            iterations_used=0,
            lineage=lineage,
            bundle=request.initial_bundle,
            contract=request.initial_contract,
            cycles=(),
            final_finding=classify_run(_run(metrics=GOOD_METRICS), RULES),
        )


def test_outcome_transport_round_trip(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        runner=FakeRunner((_run(metrics=BAD_METRICS), _run(metrics=GOOD_METRICS))),
        proposer=FakeProposer((_proposal(),)),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    document = outcome.as_dict()
    assert document["phase"] == RcaLoopPhase.COMPLETED.value
    assert document["disposition"] == RcaLoopDisposition.PASSED.value
    assert document["revisionsUsed"] == 1
    assert isinstance(outcome, RcaRevisionLoopOutcome)
    assert outcome.cycles[0].finding.signals.as_dict()["evaluations"]
