from __future__ import annotations

from typing import cast

import pytest

from thesis.experiments import (
    DOWNGRADE_TARGETS,
    EXPOSURE_MODIFIERS,
    EvidenceClaim,
    EvidenceGrade,
    EvidenceModifier,
    ExposureKind,
    downgrade,
)
from thesis.registry.records import ModelCapability
from thesis.registry.tiers import (
    CUTOFF_EXPOSURE_KIND,
    MAX_CLAIM_GRADE,
    RCA_DENIED_CAPABILITIES,
    SECOND_HIGHEST,
    TIER_PRECEDENCE,
    RcaEvidenceTier,
    TierPolicyError,
    check_tier_grade,
    effective_capabilities,
    required_tier,
    requires_attested_cutoff,
)

NOW = "2026-01-01T00:00:00.000000Z"


def _claim() -> EvidenceClaim:
    return EvidenceClaim(
        claim_id="claim-rca",
        workspace_id="workspace-a",
        contract_sha256="a" * 64,
        experiment_id="experiment-rca",
        statement="The protected run failed because of a data defect.",
        grade=EvidenceGrade.SEALED_HISTORICAL,
        created_by="evidence-service",
        created_at=NOW,
    )


def test_cutoff_constrained_is_the_second_highest_tier() -> None:
    ordered = sorted(RcaEvidenceTier, key=lambda tier: TIER_PRECEDENCE[tier])
    assert ordered == [
        RcaEvidenceTier.ADVISORY,
        RcaEvidenceTier.CUTOFF_CONSTRAINED,
        RcaEvidenceTier.SEALED_DIAGNOSTIC,
    ]
    assert SECOND_HIGHEST is RcaEvidenceTier.CUTOFF_CONSTRAINED
    assert requires_attested_cutoff(RcaEvidenceTier.ADVISORY) is False
    assert requires_attested_cutoff(RcaEvidenceTier.CUTOFF_CONSTRAINED) is True
    assert requires_attested_cutoff(RcaEvidenceTier.SEALED_DIAGNOSTIC) is True


def test_check_tier_grade_uses_explicit_policy_sets() -> None:
    check_tier_grade(RcaEvidenceTier.ADVISORY, EvidenceGrade.EXPLORATORY)
    check_tier_grade(
        RcaEvidenceTier.CUTOFF_CONSTRAINED, EvidenceGrade.HISTORICAL_VALIDATION
    )
    check_tier_grade(RcaEvidenceTier.SEALED_DIAGNOSTIC, EvidenceGrade.SEALED_HISTORICAL)
    with pytest.raises(TierPolicyError):
        check_tier_grade(RcaEvidenceTier.ADVISORY, EvidenceGrade.HISTORICAL_VALIDATION)
    with pytest.raises(TierPolicyError):
        check_tier_grade(
            RcaEvidenceTier.CUTOFF_CONSTRAINED, EvidenceGrade.SEALED_HISTORICAL
        )
    for tier in RcaEvidenceTier:
        with pytest.raises(TierPolicyError):
            check_tier_grade(tier, EvidenceGrade.FORWARD_OBSERVATION)
    assert MAX_CLAIM_GRADE[RcaEvidenceTier.SEALED_DIAGNOSTIC] == (
        EvidenceGrade.SEALED_HISTORICAL
    )


def test_required_tier_selects_the_minimum_supporting_tier() -> None:
    assert required_tier(EvidenceGrade.EXPLORATORY) is RcaEvidenceTier.ADVISORY
    assert (
        required_tier(EvidenceGrade.HISTORICAL_VALIDATION)
        is RcaEvidenceTier.CUTOFF_CONSTRAINED
    )
    assert (
        required_tier(EvidenceGrade.SEALED_HISTORICAL)
        is RcaEvidenceTier.SEALED_DIAGNOSTIC
    )
    with pytest.raises(TierPolicyError):
        required_tier(EvidenceGrade.FORWARD_OBSERVATION)


def test_rca_sessions_cannot_request_forbidden_capabilities() -> None:
    assert RCA_DENIED_CAPABILITIES == frozenset(
        {
            ModelCapability.STRATEGY_GENERATION,
            ModelCapability.CANDIDATE_SELECTION,
            ModelCapability.CANDIDATE_RANKING,
            ModelCapability.STRATEGY_EDITING,
            ModelCapability.ACTIVATION_REQUEST,
            ModelCapability.WEB_BROWSING,
            ModelCapability.WORKSPACE_HISTORY,
            ModelCapability.REAL_ENTITY_MAPPINGS,
            ModelCapability.PRIOR_PROTECTED_OUTPUTS,
        }
    )
    with pytest.raises(TierPolicyError):
        effective_capabilities(
            RcaEvidenceTier.SEALED_DIAGNOSTIC,
            {ModelCapability.TEXT_GENERATION, ModelCapability.WEB_BROWSING},
        )
    assert effective_capabilities(
        RcaEvidenceTier.CUTOFF_CONSTRAINED, {ModelCapability.TEXT_GENERATION}
    ) == frozenset({ModelCapability.TEXT_GENERATION})
    with pytest.raises(TierPolicyError):
        effective_capabilities(
            RcaEvidenceTier.ADVISORY,
            cast("list[ModelCapability]", ["text_generation"]),
        )


def test_cutoff_overlap_composes_with_experiments_exposure_downgrades() -> None:
    assert CUTOFF_EXPOSURE_KIND is ExposureKind.KNOWLEDGE_EXPOSURE
    assert (
        DOWNGRADE_TARGETS[CUTOFF_EXPOSURE_KIND][EvidenceGrade.SEALED_HISTORICAL]
        is EvidenceGrade.HISTORICAL_VALIDATION
    )
    assert (
        EvidenceModifier.KNOWLEDGE_EXPOSED in EXPOSURE_MODIFIERS[CUTOFF_EXPOSURE_KIND]
    )
    downgraded = downgrade(
        _claim(),
        CUTOFF_EXPOSURE_KIND,
        reason="model cutoff overlapped the evaluation window",
        actor="evidence-service",
        recorded_at=NOW,
    )
    assert downgraded.grade is EvidenceGrade.HISTORICAL_VALIDATION
    assert EvidenceModifier.KNOWLEDGE_EXPOSED in downgraded.modifiers
