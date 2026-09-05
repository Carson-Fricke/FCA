"""Protected-test authority client, hidden windows, exposure, and sealed facts.

Thesis is the client of the external protected-test authority.  This
capability directory implements only the client boundary: immutable
submissions, preallocated hidden-window references (never contents), signed
sealed-status facts with Ed25519 verification, a deterministic injected
transport client, and the client-side exposure ledger that maps authority
facts onto the ``thesis.experiments`` evidence-grade vocabulary.
"""

from thesis.protected.client import (
    ELIGIBILITY_PATH,
    EVALUATION_PATH,
    SEALED_STATUS_PATH,
    AuthorityRequest,
    AuthorityResponse,
    AuthorityTransport,
    Clock,
    EligibilityDeniedError,
    EligibilityGrant,
    EvaluationSubmission,
    ProtectedTestClient,
    SystemClock,
)
from thesis.protected.contracts import (
    AuthorityProtocolError,
    FactConflictError,
    LineageAncestry,
    ModelRegistryIdentity,
    ProtectedTestError,
    ProtectedTestSubmission,
    RegistryRole,
    SealedStatusKind,
    SignatureVerificationError,
    SubmissionError,
)
from thesis.protected.exposure import (
    FACT_EXPOSURE_KINDS,
    ExposureChannel,
    ExposureConflictError,
    ExposureEntry,
    ExposureError,
    ExposureLedger,
    ProtectedExposureRecord,
    downgrade_for_fact,
    exposure_kind_for_fact,
    influence_exposure_kind,
    modifiers_for,
    verify_exposure_chain,
    window_influence_modifier,
)
from thesis.protected.facts import (
    FACT_SIGNING_DOMAIN,
    AuthorityVerifier,
    SealedStatusFact,
    SealedStatusMirror,
    SignedSealedStatusFact,
    fact_signing_bytes,
)
from thesis.protected.windows import HiddenWindowRef, WindowAllocation

__all__ = [
    "ELIGIBILITY_PATH",
    "EVALUATION_PATH",
    "FACT_EXPOSURE_KINDS",
    "FACT_SIGNING_DOMAIN",
    "SEALED_STATUS_PATH",
    "AuthorityProtocolError",
    "AuthorityRequest",
    "AuthorityResponse",
    "AuthorityTransport",
    "AuthorityVerifier",
    "Clock",
    "EligibilityDeniedError",
    "EligibilityGrant",
    "EvaluationSubmission",
    "ExposureChannel",
    "ExposureConflictError",
    "ExposureEntry",
    "ExposureError",
    "ExposureLedger",
    "FACT_SIGNING_DOMAIN",
    "FactConflictError",
    "HiddenWindowRef",
    "LineageAncestry",
    "ModelRegistryIdentity",
    "ProtectedTestClient",
    "ProtectedTestError",
    "ProtectedTestSubmission",
    "ProtectedExposureRecord",
    "RegistryRole",
    "SEALED_STATUS_PATH",
    "SealedStatusFact",
    "SealedStatusKind",
    "SealedStatusMirror",
    "SignatureVerificationError",
    "SignedSealedStatusFact",
    "SubmissionError",
    "SystemClock",
    "WindowAllocation",
    "downgrade_for_fact",
    "exposure_kind_for_fact",
    "fact_signing_bytes",
    "influence_exposure_kind",
    "modifiers_for",
    "verify_exposure_chain",
    "window_influence_modifier",
]
