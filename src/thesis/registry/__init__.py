"""Model registry: deployment identities, cutoff attestation, and RCA tiers.

The registry is the single source of the exact provider/model deployment
identities Thesis may serve, the workflow-role restrictions each deployment
may fulfill, the frozen cutoff attestations that evidence claims reference,
and the per-session verification records proving that a session's model,
cutoff, role, and capability policy were checked at call time.

Composition seams:

- :mod:`thesis.agent` — registry records restrict the model roles of the
  agent model-role policy, and :func:`model_profile_for` builds the agent
  ``ModelProfile`` from a registry-verified record and attestation.
- :mod:`thesis.experiments` — :mod:`thesis.registry.tiers` maps RCA session
  tiers to the evidence grades a session's output may support.
"""

from thesis.registry.attestation import (
    REQUIRED_ATTESTED_ASPECTS,
    AttestationConflictError,
    AttestationError,
    AttestationRegistry,
    AttestedAspect,
    CutoffAttestation,
    CutoffVerification,
    verify_cutoff,
)
from thesis.registry.models import (
    ModelRegistry,
    ModelSession,
    ModelSessionRecord,
    ModelSessionVerifier,
    RegistryConflictError,
    SessionLedgerEntry,
    SessionVerificationError,
    model_profile_for,
)
from thesis.registry.records import (
    DeploymentStatus,
    ModelCapability,
    ModelRegistryRecord,
    RegistryError,
    RoleRestrictionError,
)
from thesis.registry.tiers import (
    CLAIM_GRADE_POLICY,
    CUTOFF_EXPOSURE_KIND,
    MAX_CLAIM_GRADE,
    RCA_BASE_CAPABILITIES,
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

__all__ = [
    "AttestationConflictError",
    "AttestationError",
    "AttestationRegistry",
    "AttestedAspect",
    "CLAIM_GRADE_POLICY",
    "CUTOFF_EXPOSURE_KIND",
    "CutoffAttestation",
    "CutoffVerification",
    "DeploymentStatus",
    "MAX_CLAIM_GRADE",
    "ModelCapability",
    "ModelRegistry",
    "ModelRegistryRecord",
    "ModelSession",
    "ModelSessionRecord",
    "ModelSessionVerifier",
    "RCA_BASE_CAPABILITIES",
    "RCA_DENIED_CAPABILITIES",
    "REQUIRED_ATTESTED_ASPECTS",
    "RcaEvidenceTier",
    "RegistryConflictError",
    "RegistryError",
    "RoleRestrictionError",
    "SECOND_HIGHEST",
    "SessionLedgerEntry",
    "SessionVerificationError",
    "TIER_PRECEDENCE",
    "TierPolicyError",
    "check_tier_grade",
    "effective_capabilities",
    "model_profile_for",
    "required_tier",
    "requires_attested_cutoff",
    "verify_cutoff",
]
