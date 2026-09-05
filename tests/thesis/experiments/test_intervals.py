from __future__ import annotations

import pytest

from thesis.experiments import (
    DependencyDeclaration,
    EvaluatedWindow,
    IntervalAuditReport,
    IntervalFindingKind,
    IntervalKind,
    IntervalWindow,
    ObservedConsumption,
    ProducedWindow,
    StoppingRule,
    StoppingRuleKind,
    audit_intervals,
    derive_purge_and_embargo,
)

NOW = "2026-01-01T00:00:00.000000Z"


def _declaration(
    declaration_id: str,
    consumer: str,
    producer: str,
    *windows: IntervalWindow,
) -> DependencyDeclaration:
    return DependencyDeclaration(
        declaration_id=declaration_id,
        consumer_experiment_id=consumer,
        producer_experiment_id=producer,
        windows=windows,
        declared_by="agent-1",
        declared_at=NOW,
    )


def test_purge_and_embargo_derive_from_the_worst_relevant_window() -> None:
    derived = derive_purge_and_embargo(
        (
            IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=30),
            IntervalWindow(kind=IntervalKind.POSITION, start_ns=0, end_ns=10),
            IntervalWindow(kind=IntervalKind.REVISED_DATA, start_ns=0, end_ns=20),
            IntervalWindow(kind=IntervalKind.LIQUIDATION, start_ns=0, end_ns=50),
            IntervalWindow(kind=IntervalKind.LINKED_INSTRUMENT, start_ns=0, end_ns=40),
            IntervalWindow(kind=IntervalKind.WARM_UP, start_ns=0, end_ns=500),
        )
    )
    assert derived.purge_ns == 30
    assert derived.embargo_ns == 50
    assert derived.purge_basis == (
        IntervalKind.LABEL,
        IntervalKind.POSITION,
        IntervalKind.REVISED_DATA,
    )
    assert derived.embargo_basis == (
        IntervalKind.LINKED_INSTRUMENT,
        IntervalKind.LIQUIDATION,
    )


def test_purge_and_embargo_are_zero_without_relevant_windows() -> None:
    derived = derive_purge_and_embargo(
        (IntervalWindow(kind=IntervalKind.LOOKBACK, start_ns=0, end_ns=100),)
    )
    assert derived.purge_ns == 0
    assert derived.embargo_ns == 0
    assert derived.purge_basis == ()
    assert derived.embargo_basis == ()


def test_declared_consumption_passes_the_audit() -> None:
    declaration = _declaration(
        "decl-1",
        "experiment-b",
        "experiment-a",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=100),
    )
    report = audit_intervals(
        declarations=(declaration,),
        observed_consumptions=(
            ObservedConsumption(
                consumer_experiment_id="experiment-b",
                producer_experiment_id="experiment-a",
                window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=10, end_ns=50),
            ),
        ),
        produced_windows=(
            ProducedWindow(
                experiment_id="experiment-a",
                window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=100),
            ),
        ),
    )
    assert report.clean is True
    assert report.findings == ()


def test_undeclared_consumption_is_detected() -> None:
    report = audit_intervals(
        observed_consumptions=(
            ObservedConsumption(
                consumer_experiment_id="experiment-b",
                producer_experiment_id="experiment-a",
                window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=50),
            ),
        ),
    )
    assert report.clean is False
    kinds = {finding.kind for finding in report.findings}
    assert kinds == {IntervalFindingKind.UNDECLARED_DEPENDENCY}
    finding = report.findings[0]
    assert finding.consumer_experiment_id == "experiment-b"
    assert finding.producer_experiment_id == "experiment-a"


def test_partially_covered_consumption_is_still_undeclared() -> None:
    declaration = _declaration(
        "decl-1",
        "experiment-b",
        "experiment-a",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=40),
    )
    report = audit_intervals(
        declarations=(declaration,),
        observed_consumptions=(
            ObservedConsumption(
                consumer_experiment_id="experiment-b",
                producer_experiment_id="experiment-a",
                window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=50),
            ),
        ),
    )
    assert report.clean is False
    assert report.findings[0].kind is IntervalFindingKind.UNDECLARED_DEPENDENCY


def test_overlapping_label_windows_across_producers_are_flagged() -> None:
    declaration = _declaration(
        "decl-1",
        "experiment-c",
        "experiment-a",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=100),
    )
    other = _declaration(
        "decl-2",
        "experiment-c",
        "experiment-b",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=50, end_ns=150),
    )
    report = audit_intervals(declarations=(declaration, other))
    overlaps = [
        finding
        for finding in report.findings
        if finding.kind is IntervalFindingKind.OVERLAPPING_DEPENDENCY
    ]
    assert len(overlaps) == 1
    assert overlaps[0].start_ns == 50
    assert overlaps[0].end_ns == 100


def test_declared_embargo_covers_the_overlap() -> None:
    declaration = _declaration(
        "decl-1",
        "experiment-c",
        "experiment-a",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=100),
        IntervalWindow(kind=IntervalKind.LINKED_INSTRUMENT, start_ns=50, end_ns=100),
    )
    other = _declaration(
        "decl-2",
        "experiment-c",
        "experiment-b",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=50, end_ns=150),
    )
    report = audit_intervals(declarations=(declaration, other))
    overlaps = [
        finding
        for finding in report.findings
        if finding.kind is IntervalFindingKind.OVERLAPPING_DEPENDENCY
    ]
    assert overlaps == []


def test_evaluated_window_touching_undeclared_producer_is_flagged() -> None:
    produced = ProducedWindow(
        experiment_id="experiment-a",
        window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=200, end_ns=300),
    )
    evaluated = EvaluatedWindow(
        consumer_experiment_id="experiment-b",
        window=IntervalWindow(kind=IntervalKind.LOOKBACK, start_ns=250, end_ns=400),
    )
    report = audit_intervals(
        produced_windows=(produced,),
        evaluated_windows=(evaluated,),
    )
    boundaries = [
        finding
        for finding in report.findings
        if finding.kind is IntervalFindingKind.UNDECLARED_BOUNDARY
    ]
    assert len(boundaries) == 1
    assert boundaries[0].producer_experiment_id == "experiment-a"
    assert boundaries[0].start_ns == 250
    assert boundaries[0].end_ns == 300


def test_declared_boundary_consumption_is_not_flagged() -> None:
    declaration = _declaration(
        "decl-1",
        "experiment-b",
        "experiment-a",
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=200, end_ns=300),
    )
    produced = ProducedWindow(
        experiment_id="experiment-a",
        window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=200, end_ns=300),
    )
    evaluated = EvaluatedWindow(
        consumer_experiment_id="experiment-b",
        window=IntervalWindow(kind=IntervalKind.LOOKBACK, start_ns=250, end_ns=400),
    )
    report = audit_intervals(
        declarations=(declaration,),
        produced_windows=(produced,),
        evaluated_windows=(evaluated,),
    )
    assert report.clean is True


def test_look_between_a_produced_window_and_its_neighbor_is_flagged() -> None:
    produced_first = ProducedWindow(
        experiment_id="experiment-a",
        window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=100),
    )
    produced_second = ProducedWindow(
        experiment_id="experiment-c",
        window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=100, end_ns=200),
    )
    evaluated = EvaluatedWindow(
        consumer_experiment_id="experiment-b",
        window=IntervalWindow(kind=IntervalKind.LOOKBACK, start_ns=90, end_ns=110),
    )
    report = audit_intervals(
        produced_windows=(produced_first, produced_second),
        evaluated_windows=(evaluated,),
    )
    boundaries = [
        finding
        for finding in report.findings
        if finding.kind is IntervalFindingKind.UNDECLARED_BOUNDARY
    ]
    assert {finding.producer_experiment_id for finding in boundaries} == {
        "experiment-a",
        "experiment-c",
    }


def test_exceeded_look_budget_and_missing_rule_are_flagged() -> None:
    rule = StoppingRule(
        kind=StoppingRuleKind.MAX_LOOKS,
        description="Two looks.",
        max_looks=2,
    )
    report = audit_intervals(
        stopping_rules={
            "experiment-a": rule,
            "experiment-b": rule,
            "experiment-c": rule,
        },
        look_counts={"experiment-a": 3, "experiment-b": 1, "experiment-d": 2},
    )
    exposures = [
        finding
        for finding in report.findings
        if finding.kind is IntervalFindingKind.MULTIPLE_TESTING_EXPOSURE
    ]
    assert len(exposures) == 2
    assert {finding.consumer_experiment_id for finding in exposures} == {
        "experiment-a",
        "experiment-d",
    }


def test_findings_are_deterministically_ordered() -> None:
    produced = ProducedWindow(
        experiment_id="experiment-a",
        window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=100),
    )
    evaluated = EvaluatedWindow(
        consumer_experiment_id="experiment-b",
        window=IntervalWindow(kind=IntervalKind.LOOKBACK, start_ns=0, end_ns=50),
    )
    consumed = ObservedConsumption(
        consumer_experiment_id="experiment-b",
        producer_experiment_id="experiment-a",
        window=IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=50),
    )
    rule = StoppingRule(
        kind=StoppingRuleKind.MAX_LOOKS,
        description="One look.",
        max_looks=1,
    )
    inputs = {
        "declarations": (),
        "observed_consumptions": (consumed,),
        "produced_windows": (produced,),
        "evaluated_windows": (evaluated,),
        "stopping_rules": {"experiment-b": rule},
        "look_counts": {"experiment-b": 4},
    }
    first = audit_intervals(**inputs)
    second = audit_intervals(**inputs)
    assert first == second
    kinds = [finding.kind for finding in first.findings]
    assert kinds == sorted(kinds, key=lambda kind: kind.value)


def test_self_dependency_and_empty_declarations_are_rejected() -> None:
    with pytest.raises(ValueError):
        _declaration(
            "decl-1",
            "experiment-a",
            "experiment-a",
            IntervalWindow(kind=IntervalKind.LABEL, start_ns=0, end_ns=10),
        )
    with pytest.raises(ValueError):
        DependencyDeclaration(
            declaration_id="decl-2",
            consumer_experiment_id="experiment-a",
            producer_experiment_id="experiment-b",
            windows=(),
            declared_by="agent-1",
            declared_at=NOW,
        )


def test_degenerate_windows_are_rejected() -> None:
    with pytest.raises(ValueError):
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=10, end_ns=10)
    with pytest.raises(ValueError):
        IntervalWindow(kind=IntervalKind.LABEL, start_ns=20, end_ns=10)


def test_default_report_is_clean() -> None:
    assert audit_intervals() == IntervalAuditReport(findings=())
    assert audit_intervals().clean is True
