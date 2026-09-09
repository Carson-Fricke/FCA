"""Consent grants, activation requests, and the gatekeeper client boundary.

Thesis can propose activation consent, confirm it only through explicit
structured input, build activation requests from confirmed grants, and ask
the separately owned deterministic gatekeeper to decide.  The package holds
no credentials and never decides activation itself; paper, shadow, and live
are disabled by default, natural-language ambiguity can never create
activation authority, and every step is auditable through canonical JSON v1
records.
"""

from thesis.activation.consent import (
    DEFAULT_ACTIONS_DISABLED,
    RISK_BOUND_KIND,
    ConsentConfirmation,
    ConsentError,
    ConsentGrant,
    ConsentProposal,
    GrantAction,
    GrantEnvironment,
    GrantScope,
    GrantScopeKind,
    RevokedGrant,
    confirm_grant,
    interval_scope,
    is_active,
    one_shot_scope,
    propose_grant,
    revoke,
)
from thesis.activation.gatekeeper_client import (
    ActivationState,
    ActivationStatus,
    GatekeeperClient,
    GatekeeperDecision,
    GatekeeperDecisionKind,
    GatekeeperEndpoint,
    GatekeeperError,
    GatekeeperProtocolError,
    GatekeeperRequest,
    GatekeeperRequestError,
)
from thesis.activation.requests import (
    ActivationRequest,
    ActivationRequestError,
    GrantLedger,
    GrantValidation,
    build_activation_request,
    validate_for_grant,
)

__all__ = [
    "ActivationRequest",
    "ActivationRequestError",
    "ActivationState",
    "ActivationStatus",
    "ConsentConfirmation",
    "ConsentError",
    "ConsentGrant",
    "ConsentProposal",
    "DEFAULT_ACTIONS_DISABLED",
    "GatekeeperClient",
    "GatekeeperDecision",
    "GatekeeperDecisionKind",
    "GatekeeperEndpoint",
    "GatekeeperError",
    "GatekeeperProtocolError",
    "GatekeeperRequest",
    "GatekeeperRequestError",
    "GrantAction",
    "GrantEnvironment",
    "GrantLedger",
    "GrantScope",
    "GrantScopeKind",
    "GrantValidation",
    "RISK_BOUND_KIND",
    "RevokedGrant",
    "build_activation_request",
    "confirm_grant",
    "interval_scope",
    "is_active",
    "one_shot_scope",
    "propose_grant",
    "revoke",
    "validate_for_grant",
]
