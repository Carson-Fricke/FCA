from __future__ import annotations

import pytest
from fake_authority import BUNDLE, CONTRACT, FIXED_NOW

from thesis.experiments import (
    EvidenceClaim,
    EvidenceGrade,
    EvidenceModifier,
    ExposureKind,
    InfluenceModifier,
    InfluenceSource,
)
from thesis.protected import (
    FACT_EXPOSURE_KINDS,
    ExposureChannel,
    ExposureConflictError,
    ExposureError,
    ExposureLedger,
    ProtectedExposureRecord,
    SealedStatusFact,
    SealedStatusKind,
    downgrade_for_fact,
    exposure_kind_for_fact,
    influence_exposure_kind,
    modifiers_for,
    verify_exposure_chain,
    window_influence_modifier,
)

NOW = "2026-03-01T12:00:00.000000Z"


def _record(
    exposure_id: str,
    window_id: str,
    *,
    submission_id: str = "sub-1",
    channel: ExposureChannel = ExposureChannel.PROTECTED_RUNNER,
) -> ProtectedExposureRecord:
    return ProtectedExposureRecord(
        exposure_id=exposure_id,
        workspace_id="ws-test",
        experiment_id="experiment-1",
        submission_id=submission_id,
        window_id=window_id,
        contract_sha256=CONTRACT,
        strategy_bundle_sha256=BUNDLE,
        lineage_id="lineage-1",
        channel=channel,
        execution_id="exec-1",
        exposed_at=NOW,
        description="Protected result released to the named actor.",
    )


def _fact(
    fact_id: str,
    kind: SealedStatusKind,
    *,
    payload: dict[str, object] | None = None,
    window_id: str | None = "win-0001",
) -> SealedStatusFact:
    return SealedStatusFact(
        fact_id=fact_id,
        kind=kind,
        workspace_id="ws-test",
        submission_id="sub-1",
        window_id=window_id,
        issued_at=FIXED_NOW,
        payload=payload if payload is not None else {},
    )


def _claim(grade: EvidenceGrade = EvidenceGrade.SEALED_HISTORICAL) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id="claim-1",
        workspace_id="ws-test",
        contract_sha256=CONTRACT,
        experiment_id="experiment-1",
        statement="The strategy beats its benchmark out of sample.",
        grade=grade,
        created_by="agent-1",
        created_at=NOW,
    )


def test_exposure_ledger_appends_a_verifiable_hash_chain() -> None:
    ledger = ExposureLedger()
    first = ledger.append(_record("exp-1", "win-0001"))
    second = ledger.append(_record("exp-2", "win-0002"))

    assert [entry.sequence for entry in ledger.entries()] == [1, 2]
    assert first.previous_hash == "0" * 64
    assert second.previous_hash == first.entry_hash
    assert first.exposure_kind is ExposureKind.SELECTION_EXPOSURE
    assert second.exposure_kind is ExposureKind.SELECTION_EXPOSURE
    ledger.verify()
    verify_exposure_chain(ledger.entries())


def test_reappending_identical_exposure_is_idempotent() -> None:
    ledger = ExposureLedger()
    first = ledger.append(_record("exp-1", "win-0001"))

    replay = ledger.append(_record("exp-1", "win-0001"))

    assert replay is first
    assert len(ledger.entries()) == 1
    with pytest.raises(ExposureConflictError):
        ledger.append(_record("exp-1", "win-0009"))


def test_window_once_exposed_is_marked_reused() -> None:
    ledger = ExposureLedger()
    ledger.append(_record("exp-1", "win-0001", submission_id="sub-1"))
    reused = ledger.append(_record("exp-2", "win-0001", submission_id="sub-2"))

    assert reused.exposure_kind is ExposureKind.ADAPTIVE_REUSE
    assert ledger.exposure_count("win-0001") == 2
    assert ledger.is_reused("win-0001")
    assert ledger.is_reused("win-0001", submission_id="sub-3")
    assert ledger.is_reused("win-0001", submission_id="sub-1")
    assert not ledger.is_reused("win-0002")

    solo = ledger.append(_record("exp-3", "win-0003", submission_id="sub-1"))
    assert solo.exposure_kind is ExposureKind.SELECTION_EXPOSURE
    assert ledger.is_reused("win-0003")
    assert not ledger.is_reused("win-0003", submission_id="sub-1")
    assert ledger.reused_windows() == ("win-0001",)


def test_explicit_fact_kind_overrides_the_derived_kind() -> None:
    ledger = ExposureLedger()
    entry = ledger.append(
        _record("exp-1", "win-0001"),
        kind=ExposureKind.UNKNOWN_PRIOR_EXPOSURE,
    )

    assert entry.exposure_kind is ExposureKind.UNKNOWN_PRIOR_EXPOSURE


def test_fact_mapping_matches_the_established_vocabulary() -> None:
    assert FACT_EXPOSURE_KINDS == {
        SealedStatusKind.ELIGIBILITY_GRANTED: None,
        SealedStatusKind.ELIGIBILITY_DENIED: None,
        SealedStatusKind.ELIGIBILITY_EXHAUSTED: None,
        SealedStatusKind.WINDOW_EXPOSED: ExposureKind.SELECTION_EXPOSURE,
        SealedStatusKind.WINDOW_REUSED: ExposureKind.ADAPTIVE_REUSE,
        SealedStatusKind.INFLUENCE_RECORDED: None,
        SealedStatusKind.UNKNOWN_PRIOR_EXPOSURE: ExposureKind.UNKNOWN_PRIOR_EXPOSURE,
        SealedStatusKind.SEALED_STATUS: None,
    }
    assert (
        exposure_kind_for_fact(_fact("fact-1", SealedStatusKind.WINDOW_EXPOSED))
        is ExposureKind.SELECTION_EXPOSURE
    )
    assert (
        exposure_kind_for_fact(_fact("fact-2", SealedStatusKind.WINDOW_REUSED))
        is ExposureKind.ADAPTIVE_REUSE
    )
    assert (
        exposure_kind_for_fact(_fact("fact-3", SealedStatusKind.ELIGIBILITY_GRANTED))
        is None
    )


def test_influence_facts_refine_by_declared_modifier() -> None:
    unknown = exposure_kind_for_fact(
        _fact(
            "fact-1",
            SealedStatusKind.INFLUENCE_RECORDED,
            payload={"influenceModifier": InfluenceModifier.UNKNOWN.value},
        )
    )
    exposed = exposure_kind_for_fact(
        _fact(
            "fact-2",
            SealedStatusKind.INFLUENCE_RECORDED,
            payload={"influenceModifier": InfluenceModifier.EXPOSED.value},
        )
    )
    characterized = exposure_kind_for_fact(
        _fact(
            "fact-3",
            SealedStatusKind.INFLUENCE_RECORDED,
            payload={"influenceModifier": InfluenceModifier.NONE.value},
        )
    )

    assert unknown is ExposureKind.INFLUENCE_UNKNOWN
    assert exposed is ExposureKind.KNOWLEDGE_EXPOSURE
    assert characterized is None
    with pytest.raises(ExposureError):
        exposure_kind_for_fact(
            _fact("fact-4", SealedStatusKind.INFLUENCE_RECORDED, payload={})
        )
    with pytest.raises(ExposureError):
        exposure_kind_for_fact(
            _fact(
                "fact-5",
                SealedStatusKind.INFLUENCE_RECORDED,
                payload={"influenceModifier": "post-window"},
            )
        )


def test_reuse_downgrades_a_sealed_claim_to_exploratory() -> None:
    claim = _claim()

    downgraded = downgrade_for_fact(
        claim,
        _fact("fact-1", SealedStatusKind.WINDOW_REUSED),
        reason="Second look at the same protected window.",
        actor="thesis-protected-client",
    )

    assert downgraded.grade is EvidenceGrade.EXPLORATORY
    assert downgraded.version == claim.version + 1
    assert EvidenceModifier.REUSED in downgraded.modifiers
    assert EvidenceModifier.ADAPTIVE_SELECTION in downgraded.modifiers
    assert downgraded.downgrade_history[-1].recorded_at == FIXED_NOW


def test_first_exposure_downgrades_a_sealed_claim_to_exploratory() -> None:
    downgraded = downgrade_for_fact(
        _claim(),
        _fact("fact-1", SealedStatusKind.WINDOW_EXPOSED),
        reason="Protected result released to the main agent.",
        actor="thesis-protected-client",
    )

    assert downgraded.grade is EvidenceGrade.EXPLORATORY
    assert EvidenceModifier.SELECTION_EXPOSED in downgraded.modifiers


def test_unknown_influence_downgrades_sealed_to_historical_validation() -> None:
    downgraded = downgrade_for_fact(
        _claim(),
        _fact(
            "fact-1",
            SealedStatusKind.INFLUENCE_RECORDED,
            payload={"influenceModifier": InfluenceModifier.UNKNOWN.value},
        ),
        reason="Influence provenance is incomplete.",
        actor="thesis-protected-client",
    )

    assert downgraded.grade is EvidenceGrade.HISTORICAL_VALIDATION
    assert EvidenceModifier.INFLUENCE_UNKNOWN in downgraded.modifiers


def test_unknown_prior_exposure_records_modifier_without_grade_change() -> None:
    claim = _claim()

    downgraded = downgrade_for_fact(
        claim,
        _fact("fact-1", SealedStatusKind.UNKNOWN_PRIOR_EXPOSURE),
        reason="Prior external exposure could not be established.",
        actor="thesis-protected-client",
    )

    assert downgraded.grade is EvidenceGrade.SEALED_HISTORICAL
    assert downgraded.version == claim.version + 1
    assert EvidenceModifier.UNKNOWN_PRIOR_EXPOSURE in downgraded.modifiers


def test_non_exposure_facts_leave_the_claim_unchanged() -> None:
    claim = _claim()

    unchanged = downgrade_for_fact(
        claim,
        _fact("fact-1", SealedStatusKind.ELIGIBILITY_GRANTED),
        reason="Budget granted only.",
        actor="thesis-protected-client",
    )

    assert unchanged == claim


def test_modifiers_for_reuse_comes_from_the_experiments_table() -> None:
    assert modifiers_for(ExposureKind.ADAPTIVE_REUSE) == (
        EvidenceModifier.REUSED,
        EvidenceModifier.ADAPTIVE_SELECTION,
    )
    assert modifiers_for(ExposureKind.SELECTION_EXPOSURE) == (
        EvidenceModifier.SELECTION_EXPOSED,
    )


def test_window_influence_modifier_composes_with_the_ledger_rule() -> None:
    assert (
        window_influence_modifier(
            InfluenceSource.MODEL, provenance_known=False, post_window=False
        )
        is InfluenceModifier.UNKNOWN
    )
    assert (
        window_influence_modifier(
            InfluenceSource.HUMAN, provenance_known=True, post_window=True
        )
        is InfluenceModifier.EXPOSED
    )
    assert (
        window_influence_modifier(
            InfluenceSource.RETRIEVAL, provenance_known=True, post_window=False
        )
        is InfluenceModifier.NONE
    )
    assert influence_exposure_kind(InfluenceModifier.UNKNOWN) is (
        ExposureKind.INFLUENCE_UNKNOWN
    )
    assert influence_exposure_kind(InfluenceModifier.EXPOSED) is (
        ExposureKind.KNOWLEDGE_EXPOSURE
    )
    assert influence_exposure_kind(InfluenceModifier.NONE) is None


def test_exposure_channels_record_which_actor_was_exposed() -> None:
    ledger = ExposureLedger()
    ledger.append(
        _record("exp-1", "win-0001", channel=ExposureChannel.PROTECTED_RUNNER)
    )
    ledger.append(_record("exp-2", "win-0002", channel=ExposureChannel.RCA_SESSION))
    ledger.append(_record("exp-3", "win-0003", channel=ExposureChannel.MAIN_AGENT))

    assert [entry.record.channel for entry in ledger.entries()] == [
        ExposureChannel.PROTECTED_RUNNER,
        ExposureChannel.RCA_SESSION,
        ExposureChannel.MAIN_AGENT,
    ]
