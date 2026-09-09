"""Activation requests validated against a confirmed consent grant.

An activation request is Thesis's structured ask to run one strategy bundle
under one confirmed grant.  Validation is exact and fail-closed: every field
must match the grant — grant reference, bundle hash, environment, action,
and instruments as a non-empty subset — and the grant must be unrevoked,
unexpired, inside its interval when scoped, and unconsumed for one-shot
scope.  A validated one-shot request consumes its grant in the ledger, so a
one-shot grant can never authorize a second activation.

The :class:`GrantLedger` is the authoritative revocation and consumption
view for activation checks: revocations recorded here fail every later
validation of the revoked grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from thesis.activation.consent import (
    _SYMBOL,
    ConsentError,
    ConsentGrant,
    GrantAction,
    GrantEnvironment,
    GrantScopeKind,
    RevokedGrant,
    _aware_datetime,
    _canonical_json,
    _digest,
    _instruments,
    _timestamp_text,
    is_active,
)

_SCHEMA_REQUEST: Final = "fca-thesis-activation-request-v1"
_SCHEMA_VALIDATION: Final = "fca-thesis-grant-validation-v1"


class ActivationRequestError(ValueError):
    """An activation request is invalid or does not exactly match its grant."""


def _request_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise ActivationRequestError(f"{label} must be a safe request reference")
    return value


@dataclass(frozen=True, slots=True)
class ActivationRequest:
    """The structured request to activate a bundle under a confirmed grant.

    ``grant_id`` references the confirmed :class:`ConsentGrant`; the bundle
    hash, environment, action, and instruments are re-stated by the caller
    and must match the grant exactly when validated.
    """

    request_ref: str
    grant_id: str
    bundle_hash: str
    environment: GrantEnvironment
    action: GrantAction
    instruments: tuple[str, ...]
    requested_at: datetime
    schema: str = _SCHEMA_REQUEST

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "request_ref", _request_ref(self.request_ref, "request_ref")
            )
            object.__setattr__(self, "grant_id", _digest(self.grant_id, "grant_id"))
            object.__setattr__(
                self, "bundle_hash", _digest(self.bundle_hash, "bundle_hash")
            )
            if not isinstance(self.environment, GrantEnvironment):
                raise ActivationRequestError("environment must be a GrantEnvironment")
            if not isinstance(self.action, GrantAction):
                raise ActivationRequestError("action must be a GrantAction")
            object.__setattr__(
                self, "instruments", _instruments(self.instruments, "instruments")
            )
            object.__setattr__(
                self,
                "requested_at",
                _aware_datetime(self.requested_at, "requested_at"),
            )
        except ConsentError as error:
            raise ActivationRequestError(str(error)) from error

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this request."""
        return {
            "action": self.action.value,
            "bundleHash": self.bundle_hash,
            "environment": self.environment.value,
            "grantId": self.grant_id,
            "instruments": list(self.instruments),
            "requestedAt": _timestamp_text(self.requested_at),
            "requestRef": self.request_ref,
            "schema": self.schema,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


def build_activation_request(
    grant: ConsentGrant,
    *,
    bundle_hash: str,
    instruments: tuple[str, ...],
    request_ref: str,
    requested_at: datetime,
) -> ActivationRequest:
    """Derive an activation request from a confirmed grant plus bundle metadata.

    The grant reference, environment, and action are copied verbatim from
    the grant; the bundle hash, instruments, request reference, and request
    time are supplied by the caller and must agree with the grant when the
    request is validated.

    Raises:
        ActivationRequestError: if the grant is not a :class:`ConsentGrant`
            or any supplied field is invalid.
    """
    if not isinstance(grant, ConsentGrant):
        raise ActivationRequestError("grant must be a ConsentGrant")
    return ActivationRequest(
        request_ref=request_ref,
        grant_id=grant.grant_id,
        bundle_hash=bundle_hash,
        environment=grant.environment,
        action=grant.action,
        instruments=instruments,
        requested_at=requested_at,
    )


@dataclass(frozen=True, slots=True)
class GrantValidation:
    """The audit record of one activation request validated against a grant."""

    grant_id: str
    request_ref: str
    validated_at: datetime
    consumed: bool
    schema: str = _SCHEMA_VALIDATION

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "grant_id", _digest(self.grant_id, "grant_id"))
            object.__setattr__(
                self, "request_ref", _request_ref(self.request_ref, "request_ref")
            )
            object.__setattr__(
                self,
                "validated_at",
                _aware_datetime(self.validated_at, "validated_at"),
            )
        except ConsentError as error:
            raise ActivationRequestError(str(error)) from error
        if not isinstance(self.consumed, bool):
            raise ActivationRequestError("consumed must be a bool")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this validation."""
        return {
            "consumed": self.consumed,
            "grantId": self.grant_id,
            "requestRef": self.request_ref,
            "schema": self.schema,
            "validatedAt": _timestamp_text(self.validated_at),
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


class GrantLedger:
    """A small in-memory ledger of grant revocations and one-shot consumption.

    The ledger is the local source of truth that activation checks consult
    before any activation request is forwarded: a recorded revocation fails
    every later validation of that grant, and a consumed one-shot grant
    cannot validate a second request.
    """

    def __init__(self) -> None:
        self._revocations: dict[str, RevokedGrant] = {}
        self._receipts: dict[str, GrantValidation] = {}

    def record_revocation(self, revocation: RevokedGrant) -> None:
        """Record a grant revocation.

        Raises:
            ActivationRequestError: if ``revocation`` is not a
                :class:`RevokedGrant`.
        """
        if not isinstance(revocation, RevokedGrant):
            raise ActivationRequestError("revocation must be a RevokedGrant")
        self._revocations[revocation.grant_id] = revocation

    def is_revoked(self, grant_id: str) -> bool:
        """Return whether the grant has a recorded revocation."""
        return grant_id in self._revocations

    def revocation(self, grant_id: str) -> RevokedGrant | None:
        """Return the recorded revocation for the grant, if any."""
        return self._revocations.get(grant_id)

    def revocations(self) -> tuple[RevokedGrant, ...]:
        """Return every recorded revocation for audit."""
        return tuple(self._revocations.values())

    def is_consumed(self, grant_id: str) -> bool:
        """Return whether the one-shot grant has already been consumed."""
        return grant_id in self._receipts

    def receipt(self, grant_id: str) -> GrantValidation | None:
        """Return the consumption receipt for the grant, if any."""
        return self._receipts.get(grant_id)

    def receipts(self) -> tuple[GrantValidation, ...]:
        """Return every consumption receipt for audit."""
        return tuple(self._receipts.values())

    def consume(self, validation: GrantValidation) -> None:
        """Record the consumption of a one-shot grant.

        Called by :func:`validate_for_grant` after a one-shot request has
        been fully validated.

        Raises:
            ActivationRequestError: if the receipt is not a consumed
                :class:`GrantValidation` or the grant was already consumed.
        """
        if not isinstance(validation, GrantValidation):
            raise ActivationRequestError("receipt must be a GrantValidation")
        if not validation.consumed:
            raise ActivationRequestError("only one-shot validations are consumed")
        if validation.grant_id in self._receipts:
            raise ActivationRequestError("one-shot grant has already been consumed")
        self._receipts[validation.grant_id] = validation


def validate_for_grant(
    request: ActivationRequest,
    grant: ConsentGrant,
    *,
    at: datetime,
    ledger: GrantLedger,
) -> GrantValidation:
    """Validate an activation request against its grant, fail-closed.

    The request must reference the grant and match its bundle hash,
    environment, action, and instruments (a non-empty subset); the grant
    must be unrevoked, strictly before expiration and its interval end, and
    unconsumed for one-shot scope.  A validated one-shot request consumes
    the grant in the ledger before the receipt is returned.

    Raises:
        ActivationRequestError: on any mismatch, revocation, expiry,
            interval violation, or exhausted one-shot scope.
    """
    if not isinstance(request, ActivationRequest):
        raise ActivationRequestError("request must be an ActivationRequest")
    if not isinstance(grant, ConsentGrant):
        raise ActivationRequestError("grant must be a ConsentGrant")
    if not isinstance(ledger, GrantLedger):
        raise ActivationRequestError("ledger must be a GrantLedger")
    try:
        checked_at = _aware_datetime(at, "at")
    except ConsentError as error:
        raise ActivationRequestError(str(error)) from error
    if ledger.is_revoked(grant.grant_id):
        raise ActivationRequestError("consent grant is revoked")
    if not is_active(grant, checked_at):
        raise ActivationRequestError("consent grant is expired or outside its interval")
    if request.grant_id != grant.grant_id:
        raise ActivationRequestError("activation request does not reference this grant")
    if request.bundle_hash != grant.bundle_hash:
        raise ActivationRequestError(
            "activation request bundle hash does not match the grant"
        )
    if request.environment is not grant.environment:
        raise ActivationRequestError(
            "activation request environment does not match the grant"
        )
    if request.action is not grant.action:
        raise ActivationRequestError(
            "activation request action does not match the grant"
        )
    if not set(request.instruments).issubset(grant.instruments):
        raise ActivationRequestError(
            "activation request instruments are not within the grant instruments"
        )
    if request.requested_at > checked_at:
        raise ActivationRequestError(
            "activation request is requested after validation time"
        )
    consumed = grant.scope.kind is GrantScopeKind.ONE_SHOT
    if consumed and ledger.is_consumed(grant.grant_id):
        raise ActivationRequestError("one-shot grant has already been consumed")
    validation = GrantValidation(
        grant_id=grant.grant_id,
        request_ref=request.request_ref,
        validated_at=checked_at,
        consumed=consumed,
    )
    if consumed:
        ledger.consume(validation)
    return validation


__all__ = [
    "ActivationRequest",
    "ActivationRequestError",
    "GrantLedger",
    "GrantValidation",
    "build_activation_request",
    "validate_for_grant",
]
