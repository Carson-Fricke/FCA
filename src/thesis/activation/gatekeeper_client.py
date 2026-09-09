"""Client boundary to the separately owned deterministic gatekeeper.

Thesis never decides activation.  It submits an activation request plus the
declarations the gatekeeper checks — bundle integrity, harness capability,
tests passed, environment, risk policy, and grant scope — and consumes the
published decision as activation status.  The client fails closed: every
local check runs before the endpoint is called, and any local failure means
the gatekeeper is never contacted.

The client structurally holds no credentials.  It has no authentication,
token, or secret fields, and the serialized gatekeeper request carries
declarations only.  Broker capability stays with the harness runtime; the
gatekeeper receives what it needs to verify policy, never Thesis's
credentials, and Thesis receives only activation status.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from thesis.activation.consent import (
    _DIGEST,
    RISK_BOUND_KIND,
    ConsentError,
    ConsentGrant,
    GrantAction,
    GrantEnvironment,
    GrantScopeKind,
    _aware_datetime,
    _canonical_json,
    _digest,
    _instruments,
    _positive_decimal,
    _timestamp_text,
)
from thesis.activation.requests import (
    ActivationRequest,
    ActivationRequestError,
    GrantLedger,
    validate_for_grant,
)

_SCHEMA_GATEKEEPER_REQUEST: Final = "fca-thesis-gatekeeper-request-v1"
_SCHEMA_DECISION: Final = "fca-thesis-gatekeeper-decision-v1"
_SCHEMA_STATUS: Final = "fca-thesis-activation-status-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


class GatekeeperError(ValueError):
    """Base class for gatekeeper client failures."""


class GatekeeperRequestError(GatekeeperError):
    """A local validation failed, so the endpoint was never contacted."""


class GatekeeperProtocolError(GatekeeperError):
    """The endpoint returned a decision that violates the protocol."""


class ActivationState(StrEnum):
    """The published activation state Thesis may monitor and explain."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class GatekeeperDecisionKind(StrEnum):
    """The decision kinds the deterministic gatekeeper emits."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise GatekeeperRequestError(f"{label} must be a safe identifier")
    return value


def _decision_ref(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise GatekeeperProtocolError("decision request_ref must be a safe identifier")
    return value


@dataclass(frozen=True, slots=True)
class GatekeeperRequest:
    """The declarations the deterministic gatekeeper independently checks.

    The gatekeeper never trusts these declarations; they tell it exactly
    what to verify: bundle integrity (hash), harness capability, tests
    passed, environment, risk policy (exact capital and max-daily-loss
    bounds), and grant scope.  The request intentionally has no credential,
    token, or secret field.
    """

    request_ref: str
    grant_id: str
    bundle_hash: str
    environment: GrantEnvironment
    action: GrantAction
    instruments: tuple[str, ...]
    capital_bound: Decimal
    risk_bound: Decimal
    scope_kind: GrantScopeKind
    interval_end: datetime | None
    harness_capability: str
    tests_passed: bool
    schema: str = _SCHEMA_GATEKEEPER_REQUEST

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "request_ref", _identifier(self.request_ref, "request_ref")
            )
            object.__setattr__(self, "grant_id", _digest(self.grant_id, "grant_id"))
            object.__setattr__(
                self, "bundle_hash", _digest(self.bundle_hash, "bundle_hash")
            )
            if not isinstance(self.environment, GrantEnvironment):
                raise GatekeeperRequestError("environment must be a GrantEnvironment")
            if not isinstance(self.action, GrantAction):
                raise GatekeeperRequestError("action must be a GrantAction")
            object.__setattr__(
                self, "instruments", _instruments(self.instruments, "instruments")
            )
            object.__setattr__(
                self,
                "capital_bound",
                _positive_decimal(self.capital_bound, "capital_bound"),
            )
            object.__setattr__(
                self,
                "risk_bound",
                _positive_decimal(self.risk_bound, "risk_bound"),
            )
            if not isinstance(self.scope_kind, GrantScopeKind):
                raise GatekeeperRequestError("scope_kind must be a GrantScopeKind")
            if self.scope_kind is GrantScopeKind.ONE_SHOT:
                if self.interval_end is not None:
                    raise GatekeeperRequestError(
                        "one-shot scope must not carry an interval end"
                    )
            else:
                object.__setattr__(
                    self,
                    "interval_end",
                    _aware_datetime(self.interval_end, "interval_end"),
                )
            object.__setattr__(
                self,
                "harness_capability",
                _identifier(self.harness_capability, "harness_capability"),
            )
            if not isinstance(self.tests_passed, bool):
                raise GatekeeperRequestError("tests_passed must be a bool declaration")
        except ConsentError as error:
            raise GatekeeperRequestError(str(error)) from error

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this declaration request."""
        return {
            "action": self.action.value,
            "bundleHash": self.bundle_hash,
            "capitalBound": format(self.capital_bound, "f"),
            "environment": self.environment.value,
            "grantId": self.grant_id,
            "harnessCapability": self.harness_capability,
            "instruments": list(self.instruments),
            "intervalEnd": (
                None
                if self.interval_end is None
                else _timestamp_text(self.interval_end)
            ),
            "requestRef": self.request_ref,
            "riskBound": format(self.risk_bound, "f"),
            "riskBoundKind": RISK_BOUND_KIND,
            "scopeKind": self.scope_kind.value,
            "schema": self.schema,
            "testsPassed": self.tests_passed,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


@dataclass(frozen=True, slots=True)
class GatekeeperDecision:
    """The deterministic gatekeeper's decision for one request reference."""

    request_ref: str
    decision: GatekeeperDecisionKind
    reason: str = ""
    schema: str = _SCHEMA_DECISION

    def __post_init__(self) -> None:
        if not isinstance(self.decision, GatekeeperDecisionKind):
            raise GatekeeperProtocolError("decision must be a GatekeeperDecisionKind")
        object.__setattr__(self, "request_ref", _decision_ref(self.request_ref))
        if self.decision is GatekeeperDecisionKind.REJECTED:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise GatekeeperProtocolError("a rejected decision must carry a reason")
        elif self.reason != "":
            raise GatekeeperProtocolError("only a rejected decision carries a reason")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this decision."""
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "requestRef": self.request_ref,
            "schema": self.schema,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


@dataclass(frozen=True, slots=True)
class ActivationStatus:
    """The published activation status Thesis may monitor and explain.

    Thesis receives status only — never broker capability, gatekeeper
    credentials, or order placement tools.
    """

    request_ref: str
    state: ActivationState
    reason: str = ""
    schema: str = _SCHEMA_STATUS

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_ref", _decision_ref(self.request_ref))
        if not isinstance(self.state, ActivationState):
            raise GatekeeperProtocolError("state must be an ActivationState")
        if self.state is ActivationState.REJECTED:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise GatekeeperProtocolError("a rejected status must carry a reason")
        elif self.reason != "":
            raise GatekeeperProtocolError("only a rejected status carries a reason")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this status."""
        return {
            "reason": self.reason,
            "requestRef": self.request_ref,
            "schema": self.schema,
            "state": self.state.value,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


class GatekeeperEndpoint(Protocol):
    """The transport to the gatekeeper that a client is bound to.

    Implementations own the actual call; the repository ships none, and
    tests inject in-memory fakes.  Endpoints carry no credentials: the
    protocol has no authentication surface by construction.
    """

    def submit(self, request: GatekeeperRequest) -> GatekeeperDecision:
        """Forward one declaration request to the gatekeeper."""
        ...

    def poll(self, request_ref: str) -> GatekeeperDecision:
        """Return the latest decision recorded for ``request_ref``."""
        ...


def _status_from_decision(decision: GatekeeperDecision) -> ActivationStatus:
    return ActivationStatus(
        request_ref=decision.request_ref,
        state=ActivationState(decision.decision.value),
        reason=decision.reason,
    )


class GatekeeperClient:
    """Fail-closed client for the deterministic activation gatekeeper.

    The client validates locally before every submission, forwards only
    declarations, and publishes the gatekeeper's decision as status.  Its
    only state is the injected endpoint and the local grant ledger; it has
    no credential fields of any kind.
    """

    def __init__(self, endpoint: GatekeeperEndpoint, *, ledger: GrantLedger) -> None:
        self._endpoint = endpoint
        self._ledger = ledger

    def submit(
        self,
        request: ActivationRequest,
        grant: ConsentGrant,
        *,
        at: datetime,
        harness_capability: str,
        tests_passed: bool,
    ) -> ActivationStatus:
        """Validate locally, then forward declarations to the gatekeeper.

        Every local check — bundle-hash format, expiry, revocation, scope,
        and exact request/grant agreement — runs before the endpoint is
        called; any local failure raises without contacting the gatekeeper.
        A validated one-shot request consumes its grant in the ledger at
        validation time, before the gatekeeper's decision arrives.

        Raises:
            GatekeeperRequestError: on any local validation failure.
            GatekeeperProtocolError: if the endpoint violates the protocol.
        """
        if not isinstance(tests_passed, bool):
            raise GatekeeperRequestError("tests_passed must be a bool declaration")
        capability = _identifier(harness_capability, "harness_capability")
        self._preflight(request, grant, at)
        gatekeeper_request = GatekeeperRequest(
            request_ref=request.request_ref,
            grant_id=grant.grant_id,
            bundle_hash=request.bundle_hash,
            environment=request.environment,
            action=request.action,
            instruments=request.instruments,
            capital_bound=grant.capital_bound,
            risk_bound=grant.risk_bound,
            scope_kind=grant.scope.kind,
            interval_end=grant.scope.interval_end,
            harness_capability=capability,
            tests_passed=tests_passed,
        )
        decision = self._endpoint.submit(gatekeeper_request)
        if not isinstance(decision, GatekeeperDecision):
            raise GatekeeperProtocolError("endpoint must return a GatekeeperDecision")
        if decision.request_ref != request.request_ref:
            raise GatekeeperProtocolError(
                "gatekeeper decision references a different request"
            )
        return _status_from_decision(decision)

    def poll(self, request_ref: str) -> ActivationStatus:
        """Return the published activation status for ``request_ref``.

        Raises:
            GatekeeperRequestError: if the reference is not a safe request
                reference.
            GatekeeperProtocolError: if the endpoint violates the protocol.
        """
        checked_ref = _identifier(request_ref, "request_ref")
        decision = self._endpoint.poll(checked_ref)
        if not isinstance(decision, GatekeeperDecision):
            raise GatekeeperProtocolError("endpoint must return a GatekeeperDecision")
        if decision.request_ref != checked_ref:
            raise GatekeeperProtocolError(
                "gatekeeper decision references a different request"
            )
        return _status_from_decision(decision)

    def _preflight(
        self,
        request: ActivationRequest,
        grant: ConsentGrant,
        at: datetime,
    ) -> None:
        """Run every local check; raise before the endpoint is contacted."""
        if (
            not isinstance(request.bundle_hash, str)
            or _DIGEST.fullmatch(request.bundle_hash) is None
        ):
            raise GatekeeperRequestError(
                "request bundle hash must be a lowercase 64-hex digest"
            )
        if (
            not isinstance(grant.bundle_hash, str)
            or _DIGEST.fullmatch(grant.bundle_hash) is None
        ):
            raise GatekeeperRequestError(
                "grant bundle hash must be a lowercase 64-hex digest"
            )
        try:
            validate_for_grant(request, grant, at=at, ledger=self._ledger)
        except (ActivationRequestError, ConsentError) as error:
            raise GatekeeperRequestError(str(error)) from error


__all__ = [
    "ActivationState",
    "ActivationStatus",
    "GatekeeperClient",
    "GatekeeperDecision",
    "GatekeeperDecisionKind",
    "GatekeeperEndpoint",
    "GatekeeperError",
    "GatekeeperProtocolError",
    "GatekeeperRequest",
    "GatekeeperRequestError",
]
