from __future__ import annotations

from typing import cast

import pytest

from thesis.experiments import (
    DOWNGRADE_TARGETS,
    DowngradeRecord,
    EvidenceClaim,
    EvidenceGrade,
    EvidenceGradeError,
    EvidenceModifier,
    ExposureKind,
    check_grade_transition,
    downgrade,
)

NOW = "2026-01-01T00:00:00.000000Z"
CONTRACT = "a" * 64


def _claim(
    grade: EvidenceGrade = EvidenceGrade.SEALED_HISTORICAL,
    *,
    modifiers: tuple[EvidenceModifier, ...] = (),
    version: int = 1,
    downgrade_history: tuple[DowngradeRecord, ...] = (),
) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id="claim-1",
        workspace_id="workspace-a",
        contract_sha256=CONTRACT,
        experiment_id="experiment-costs",
        statement="Holdout Sharpe exceeds the baseline by at least 0.2.",
        grade=grade,
        created_by="agent-1",
        created_at=NOW,
        modifiers=modifiers,
        artifact_references=(
            {"uri": "experiments/claim-1/metrics.json", "sha256": "b" * 64},
        ),
        limitations=("Single market, single horizon.",),
        version=version,
        downgrade_history=downgrade_history,
    )


def test_claim_hashes_canonically_and_keeps_traceability() -> None:
    claim = _claim()
    again = _claim()
    assert claim.content_sha256 == again.content_sha256
    document = claim.canonical_document
    assert document["grade"] == "sealed_historical"
    assert document["contractSha256"] == CONTRACT
    assert document["artifactReferences"] == [
        {"uri": "experiments/claim-1/metrics.json", "sha256": "b" * 64}
    ]


def test_exploratory_grade_matches_the_evidence_report_vocabulary() -> None:
    assert EvidenceGrade.EXPLORATORY.value == "exploratory"


def test_adaptive_reuse_downgrades_sealed_evidence_to_exploratory() -> None:
    claim = _claim()
    downgraded = downgrade(
        claim,
        ExposureKind.ADAPTIVE_REUSE,
        reason="Full-result adaptation after the window was released.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert downgraded.grade is EvidenceGrade.EXPLORATORY
    assert EvidenceModifier.REUSED in downgraded.modifiers
    assert EvidenceModifier.ADAPTIVE_SELECTION in downgraded.modifiers
    assert downgraded.version == 2
    assert len(downgraded.downgrade_history) == 1
    record = downgraded.downgrade_history[0]
    assert record.from_grade is EvidenceGrade.SEALED_HISTORICAL
    assert record.to_grade is EvidenceGrade.EXPLORATORY
    assert downgraded.content_sha256 != claim.content_sha256
    assert downgraded.artifact_references == claim.artifact_references


def test_downgrade_requires_a_reason() -> None:
    claim = _claim()
    with pytest.raises(EvidenceGradeError):
        downgrade(
            claim,
            ExposureKind.ADAPTIVE_REUSE,
            reason="",
            actor="agent-1",
            recorded_at=NOW,
        )


def test_steering_after_exposure_downgrades_and_cannot_restore_sealed() -> None:
    claim = _claim()
    downgraded = downgrade(
        claim,
        ExposureKind.STEERING_AFTER_EXPOSURE,
        reason="Outcome-correlated steering closed the contract version.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert downgraded.grade is EvidenceGrade.EXPLORATORY
    with pytest.raises(EvidenceGradeError):
        check_grade_transition(downgraded.grade, EvidenceGrade.SEALED_HISTORICAL)
    with pytest.raises(EvidenceGradeError):
        check_grade_transition(downgraded.grade, EvidenceGrade.HISTORICAL_VALIDATION)


def test_exploratory_claims_have_no_upward_transition() -> None:
    with pytest.raises(EvidenceGradeError):
        check_grade_transition(
            EvidenceGrade.EXPLORATORY, EvidenceGrade.FORWARD_OBSERVATION
        )
    with pytest.raises(EvidenceGradeError):
        check_grade_transition(
            EvidenceGrade.EXPLORATORY, EvidenceGrade.SEALED_HISTORICAL
        )
    with pytest.raises(EvidenceGradeError):
        check_grade_transition(
            EvidenceGrade.EXPLORATORY, EvidenceGrade.HISTORICAL_VALIDATION
        )


def test_selection_exposure_downgrades_validation_to_exploratory() -> None:
    claim = _claim(EvidenceGrade.HISTORICAL_VALIDATION)
    downgraded = downgrade(
        claim,
        ExposureKind.SELECTION_EXPOSURE,
        reason="The window informed candidate selection.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert downgraded.grade is EvidenceGrade.EXPLORATORY
    assert downgraded.modifiers == (EvidenceModifier.SELECTION_EXPOSED,)


def test_knowledge_exposure_keeps_validation_with_a_modifier() -> None:
    claim = _claim(EvidenceGrade.HISTORICAL_VALIDATION)
    downgraded = downgrade(
        claim,
        ExposureKind.KNOWLEDGE_EXPOSURE,
        reason="RCA model cutoff overlaps the evaluation window.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert downgraded.grade is EvidenceGrade.HISTORICAL_VALIDATION
    assert downgraded.modifiers == (EvidenceModifier.KNOWLEDGE_EXPOSED,)
    sealed_down = downgrade(
        _claim(),
        ExposureKind.KNOWLEDGE_EXPOSURE,
        reason="Cutoff overlap on a sealed window.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert sealed_down.grade is EvidenceGrade.HISTORICAL_VALIDATION


def test_transformation_records_a_modifier_without_changing_grade() -> None:
    claim = _claim()
    downgraded = downgrade(
        claim,
        ExposureKind.TRANSFORMATION,
        reason="Results were aliased and transformed by the authority.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert downgraded.grade is EvidenceGrade.SEALED_HISTORICAL
    assert downgraded.modifiers == (EvidenceModifier.TRANSFORMED,)
    assert downgraded.version == 2


def test_downgrades_compose_and_stay_at_the_floor() -> None:
    claim = _claim()
    once = downgrade(
        claim,
        ExposureKind.SELECTION_EXPOSURE,
        reason="Selection saw the window.",
        actor="agent-1",
        recorded_at=NOW,
    )
    twice = downgrade(
        once,
        ExposureKind.INFLUENCE_UNKNOWN,
        reason="Human influence provenance is unknown.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert twice.grade is EvidenceGrade.EXPLORATORY
    assert twice.version == 3
    assert len(twice.downgrade_history) == 2
    assert twice.modifiers == (
        EvidenceModifier.INFLUENCE_UNKNOWN,
        EvidenceModifier.SELECTION_EXPOSED,
    )
    again = downgrade(
        twice,
        ExposureKind.ADAPTIVE_REUSE,
        reason="Reused the exposed window again.",
        actor="agent-1",
        recorded_at=NOW,
    )
    assert again.grade is EvidenceGrade.EXPLORATORY
    assert EvidenceModifier.REUSED in again.modifiers


def test_downgrade_table_never_maps_a_grade_upward() -> None:
    for targets in DOWNGRADE_TARGETS.values():
        for source, target in targets.items():
            if target is None:
                continue
            check_grade_transition(source, target)


def test_claims_validate_ids_grades_and_history() -> None:
    with pytest.raises(EvidenceGradeError):
        _claim(grade=cast(EvidenceGrade, "sealed"))
    with pytest.raises(EvidenceGradeError):
        _claim(modifiers=cast(tuple[EvidenceModifier, ...], ("reused",)))
    with pytest.raises(EvidenceGradeError):
        _claim(downgrade_history=cast(tuple[DowngradeRecord, ...], ("not-a-record",)))
    record = DowngradeRecord(
        from_grade=EvidenceGrade.SEALED_HISTORICAL,
        to_grade=EvidenceGrade.EXPLORATORY,
        exposure=ExposureKind.ADAPTIVE_REUSE,
        reason="Recorded exposure.",
        actor="agent-1",
        recorded_at=NOW,
    )
    claim = _claim(downgrade_history=(record,), version=2)
    assert claim.downgrade_history == (record,)
