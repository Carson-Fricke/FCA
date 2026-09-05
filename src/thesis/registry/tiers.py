"""RCA subagent evidence tiers and their composition with evidence grades.

RCA evidence tiers classify how a model subagent's output may be used in a
protected-result diagnosis.  They are protocol tiers, separate from the
exposure grades of :mod:`thesis.experiments.grades`: the tier states which
registry-verified protocol the subagent session ran, and the policy below
maps each tier to the claim grades its output may support.

The second-highest tier, ``cutoff_constrained``, is the standard practice for
protected-result diagnosis: the session uses a registry-verified model whose
conservative attested cutoff is strictly before the earliest model-visible
observation.  A subagent cannot silently select a weaker protocol than the
requested tier, and no model subagent tier supports forward-observation
claims.

Every tier denies the same route capabilities: an RCA session cannot
generate, select, rank, edit, activate, browse, access real entity mappings,
prior protected outputs, or workspace history.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from types import MappingProxyType

from thesis.experiments.grades import EvidenceGrade, ExposureKind
from thesis.registry.records import ModelCapability, RegistryError


class TierPolicyError(RegistryError):
    """Raised when a tier, grade, or capability pairing is not permitted."""


class RcaEvidenceTier(StrEnum):
    """The registry-verified protocol tier of one RCA subagent session."""

    ADVISORY = "advisory"
    CUTOFF_CONSTRAINED = "cutoff_constrained"
    SEALED_DIAGNOSTIC = "sealed_diagnostic"


TIER_PRECEDENCE: MappingProxyType[RcaEvidenceTier, int] = MappingProxyType(
    {
        RcaEvidenceTier.ADVISORY: 1,
        RcaEvidenceTier.CUTOFF_CONSTRAINED: 2,
        RcaEvidenceTier.SEALED_DIAGNOSTIC: 3,
    }
)

SECOND_HIGHEST: RcaEvidenceTier = RcaEvidenceTier.CUTOFF_CONSTRAINED

#: Claim grades each tier's output may support.  Explicit sets, not a scalar
#: ranking: grades describe exposure conditions, not strength.
CLAIM_GRADE_POLICY: MappingProxyType[RcaEvidenceTier, frozenset[EvidenceGrade]] = (
    MappingProxyType(
        {
            RcaEvidenceTier.ADVISORY: frozenset({EvidenceGrade.EXPLORATORY}),
            RcaEvidenceTier.CUTOFF_CONSTRAINED: frozenset(
                {EvidenceGrade.EXPLORATORY, EvidenceGrade.HISTORICAL_VALIDATION}
            ),
            RcaEvidenceTier.SEALED_DIAGNOSTIC: frozenset(
                {
                    EvidenceGrade.EXPLORATORY,
                    EvidenceGrade.HISTORICAL_VALIDATION,
                    EvidenceGrade.SEALED_HISTORICAL,
                }
            ),
        }
    )
)

MAX_CLAIM_GRADE: MappingProxyType[RcaEvidenceTier, EvidenceGrade] = MappingProxyType(
    {
        RcaEvidenceTier.ADVISORY: EvidenceGrade.EXPLORATORY,
        RcaEvidenceTier.CUTOFF_CONSTRAINED: EvidenceGrade.HISTORICAL_VALIDATION,
        RcaEvidenceTier.SEALED_DIAGNOSTIC: EvidenceGrade.SEALED_HISTORICAL,
    }
)

#: Capabilities no RCA tier may grant, whatever the deployment declares.
RCA_DENIED_CAPABILITIES: frozenset[ModelCapability] = frozenset(
    {
        ModelCapability.ACTIVATION_REQUEST,
        ModelCapability.CANDIDATE_RANKING,
        ModelCapability.CANDIDATE_SELECTION,
        ModelCapability.PRIOR_PROTECTED_OUTPUTS,
        ModelCapability.REAL_ENTITY_MAPPINGS,
        ModelCapability.STRATEGY_EDITING,
        ModelCapability.STRATEGY_GENERATION,
        ModelCapability.WEB_BROWSING,
        ModelCapability.WORKSPACE_HISTORY,
    }
)

#: The default effective capability set of an RCA session: analysis text only.
RCA_BASE_CAPABILITIES: frozenset[ModelCapability] = frozenset(
    {ModelCapability.TEXT_GENERATION}
)

#: The exposure kind recorded when a model cutoff overlaps the evaluated
#: window.  ``thesis.experiments.grades`` has no dedicated ``cutoff-overlap``
#: modifier yet, so the knowledge-exposure transition is the composed record.
CUTOFF_EXPOSURE_KIND: ExposureKind = ExposureKind.KNOWLEDGE_EXPOSURE


def check_tier_grade(tier: RcaEvidenceTier, grade: EvidenceGrade) -> None:
    """Reject a claim grade that the session's tier may not support."""
    if not isinstance(tier, RcaEvidenceTier):
        raise TierPolicyError("tier must be an RcaEvidenceTier")
    if not isinstance(grade, EvidenceGrade):
        raise TierPolicyError("grade must be an EvidenceGrade")
    if grade not in CLAIM_GRADE_POLICY[tier]:
        raise TierPolicyError(
            f"{tier.value} RCA output cannot support {grade.value} claims"
        )


def required_tier(grade: EvidenceGrade) -> RcaEvidenceTier:
    """Return the minimum tier whose output may support a claim grade.

    Forward-observation claims have no model-subagent tier: they come from
    post-protocol live observation, never from an RCA session.
    """
    if not isinstance(grade, EvidenceGrade):
        raise TierPolicyError("grade must be an EvidenceGrade")
    for tier in sorted(RcaEvidenceTier, key=lambda item: TIER_PRECEDENCE[item]):
        if grade in CLAIM_GRADE_POLICY[tier]:
            return tier
    raise TierPolicyError("no RCA subagent tier supports forward-observation claims")


def requires_attested_cutoff(tier: RcaEvidenceTier) -> bool:
    """Return whether a tier demands the conservative cutoff gate."""
    if not isinstance(tier, RcaEvidenceTier):
        raise TierPolicyError("tier must be an RcaEvidenceTier")
    return TIER_PRECEDENCE[tier] >= TIER_PRECEDENCE[SECOND_HIGHEST]


def effective_capabilities(
    tier: RcaEvidenceTier,
    requested: Iterable[ModelCapability],
) -> frozenset[ModelCapability]:
    """Return the capabilities a session may exercise under its tier."""
    if not isinstance(tier, RcaEvidenceTier):
        raise TierPolicyError("tier must be an RcaEvidenceTier")
    requested_set = frozenset(requested)
    for capability in requested_set:
        if not isinstance(capability, ModelCapability):
            raise TierPolicyError("requested capabilities must be ModelCapability")
    denied = requested_set & RCA_DENIED_CAPABILITIES
    if denied:
        names = sorted(capability.value for capability in denied)
        raise TierPolicyError(f"RCA sessions cannot request capabilities: {names}")
    return requested_set
