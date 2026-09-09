# Thesis activation boundary (TH5.1)

Structured consent grants and the activation request boundary between Thesis
and the activation gatekeeper. Natural-language ambiguity can never create
activation authority: only an explicit structured
`ConsentConfirmation` restating the proposal's exact bundle hash, environment,
and action promotes a `ConsentProposal` into a `ConsentGrant`. Paper, shadow,
and live activation are disabled by default (`DEFAULT_ACTIONS_DISABLED`);
revocation and expiry are enforced on every check by `validate_for_grant`
through the `GrantLedger`. `GatekeeperClient` forwards declarations to the
deterministic gatekeeper and surfaces only `ActivationStatus` — Thesis holds
neither broker nor gatekeeper credentials.

Public API: `ConsentProposal`, `ConsentConfirmation`, `ConsentGrant`,
`RevokedGrant`, `GrantScope`, `GrantScopeKind`, `GrantEnvironment`,
`GrantAction`, `DEFAULT_ACTIONS_DISABLED`, `RISK_BOUND_KIND`,
`propose_grant`, `confirm_grant`, `revoke`, `is_active`, `one_shot_scope`,
`interval_scope`, `ConsentError`, `ActivationRequest`,
`build_activation_request`, `validate_for_grant`, `GrantValidation`,
`GrantLedger`, `ActivationRequestError`, `GatekeeperClient`,
`GatekeeperEndpoint`, `GatekeeperRequest`, `GatekeeperDecision`,
`GatekeeperDecisionKind`, `ActivationStatus`, `ActivationState`,
`GatekeeperError`, `GatekeeperRequestError`, `GatekeeperProtocolError`.
