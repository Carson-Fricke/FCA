"""Consent grants for activation: proposal, explicit confirmation, revocation.

Thesis may *propose* an activation consent, but only an explicit structured
confirmation creates a grant, and only the separately owned deterministic
gatekeeper turns a confirmed grant plus an activation request into a
decision.  Natural-language ambiguity can never create activation authority:
this module exposes no free-text path into a grant, and every value entering
a proposal, confirmation, or grant is a typed, validated field.

A grant names the exact bundle (by lowercase SHA-256 content hash), the
environment (``dev`` or ``prod``), the permitted activation action
(``paper``, ``shadow``, or ``live`` — all disabled by default), the permitted
instruments, exact capital and risk bounds, injected creation and expiration
instants, and a scope that is either one-shot or a bounded interval with an
explicit interval end.  Grants are revocable; revocation records are
first-class audit artifacts, and activation checks must reject revoked
grants.

All records serialize to canonical JSON v1 documents: compact separators,
ASCII-only, keys sorted, exact ``Decimal`` bounds rendered as plain decimal
text, and timezone-aware datetimes rendered as UTC ISO-8601 with microsecond
precision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

_SCHEMA_CONFIRMATION: Final = "fca-thesis-consent-confirmation-v1"
_SCHEMA_PROPOSAL: Final = "fca-thesis-consent-proposal-v1"
_SCHEMA_GRANT: Final = "fca-thesis-consent-grant-v1"
_SCHEMA_REVOCATION: Final = "fca-thesis-consent-revocation-v1"

_GRANT_HASH_DOMAIN: Final = b"fca:thesis-consent-grant:v1\0"

#: The single documented risk bound carried by a grant: the maximum daily
#: loss the gatekeeper must enforce for activations made under the grant.
RISK_BOUND_KIND: Final = "max-daily-loss"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{7,255}$")


class ConsentError(ValueError):
    """A consent proposal, confirmation, grant, or revocation is invalid."""


class GrantEnvironment(StrEnum):
    """The environment a grant may authorize activation into."""

    DEV = "dev"
    PROD = "prod"


class GrantAction(StrEnum):
    """The activation action a grant permits."""

    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


class GrantScopeKind(StrEnum):
    """Whether a grant authorizes one activation or a bounded interval."""

    ONE_SHOT = "one_shot"
    INTERVAL = "interval"


#: Whether each activation action is enabled before any consent grant
#: exists.  Every action maps to ``False``: paper, shadow, and live are all
#: disabled by default, only a confirmed :class:`ConsentGrant` can enable
#: one, and this mapping never changes at runtime.
DEFAULT_ACTIONS_DISABLED: Mapping[GrantAction, bool] = MappingProxyType(
    {
        GrantAction.PAPER: False,
        GrantAction.SHADOW: False,
        GrantAction.LIVE: False,
    }
)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ConsentError(f"{label} must be a lowercase 64-hex SHA-256 digest")
    return value


def _aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ConsentError(f"{label} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConsentError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _exact_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ConsentError(
            f"{label} must be an exact Decimal or int; float and text are rejected"
        )
    number = value if isinstance(value, Decimal) else Decimal(value)
    if not number.is_finite():
        raise ConsentError(f"{label} must be finite")
    return number


def _positive_decimal(value: object, label: str) -> Decimal:
    number = _exact_decimal(value, label)
    if number <= 0:
        raise ConsentError(f"{label} must be greater than zero")
    return number


def _instruments(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ConsentError(f"{label} must be a sequence of instrument symbols")
    symbols: list[str] = []
    for item in value:
        if not isinstance(item, str) or _SYMBOL.fullmatch(item) is None:
            raise ConsentError(f"{label} must contain safe instrument symbols")
        symbols.append(item)
    if not symbols:
        raise ConsentError(f"{label} must not be empty")
    return tuple(sorted(set(symbols)))


def _timestamp_text(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class GrantScope:
    """Whether a grant authorizes one activation or a bounded interval.

    A one-shot scope carries no interval end; an interval scope must carry
    an explicit interval end.
    """

    kind: GrantScopeKind
    interval_end: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, GrantScopeKind):
            raise ConsentError("scope kind must be a GrantScopeKind")
        if self.kind is GrantScopeKind.ONE_SHOT:
            if self.interval_end is not None:
                raise ConsentError("one-shot scope must not carry an interval end")
            return
        object.__setattr__(
            self, "interval_end", _aware_datetime(self.interval_end, "interval_end")
        )

    def require_interval_end(self) -> datetime:
        """Return the explicit interval end, raising for one-shot scope."""
        if self.interval_end is None:
            raise ConsentError("interval scope must carry an explicit interval end")
        return self.interval_end

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this scope."""
        return {
            "intervalEnd": (
                None
                if self.interval_end is None
                else _timestamp_text(self.interval_end)
            ),
            "kind": self.kind.value,
        }


def one_shot_scope() -> GrantScope:
    """Return the scope that authorizes exactly one validated activation."""
    return GrantScope(kind=GrantScopeKind.ONE_SHOT)


def interval_scope(interval_end: datetime) -> GrantScope:
    """Return the scope that authorizes activations until ``interval_end``."""
    return GrantScope(kind=GrantScopeKind.INTERVAL, interval_end=interval_end)


def _validated_terms(
    bundle_hash: object,
    environment: object,
    action: object,
    instruments: object,
    capital_bound: object,
    risk_bound: object,
    expiration: object,
    scope: object,
) -> tuple[
    str,
    GrantEnvironment,
    GrantAction,
    tuple[str, ...],
    Decimal,
    Decimal,
    datetime,
    GrantScope,
]:
    """Validate the terms shared by proposals and grants."""
    if not isinstance(environment, GrantEnvironment):
        raise ConsentError(
            "environment must be a GrantEnvironment; text cannot authorize activation"
        )
    if not isinstance(action, GrantAction):
        raise ConsentError(
            "action must be a GrantAction; text cannot authorize activation"
        )
    if not isinstance(scope, GrantScope):
        raise ConsentError("scope must be a GrantScope")
    return (
        _digest(bundle_hash, "bundle_hash"),
        environment,
        action,
        _instruments(instruments, "instruments"),
        _positive_decimal(capital_bound, "capital_bound"),
        _positive_decimal(risk_bound, "risk_bound"),
        _aware_datetime(expiration, "expiration"),
        scope,
    )


@dataclass(frozen=True, slots=True)
class ConsentProposal:
    """A typed activation-consent proposal awaiting explicit confirmation.

    A proposal authorizes nothing.  It is the structured artifact a chat
    turn or UI/API screen must display verbatim — exact bundle hash,
    environment, action, bounds, expiration, and scope — before an operator
    can restate it in a :class:`ConsentConfirmation`.
    """

    bundle_hash: str
    environment: GrantEnvironment
    action: GrantAction
    instruments: tuple[str, ...]
    capital_bound: Decimal
    risk_bound: Decimal
    expiration: datetime
    scope: GrantScope
    proposed_at: datetime
    schema: str = _SCHEMA_PROPOSAL

    def __post_init__(self) -> None:
        (
            bundle_hash,
            environment,
            action,
            instruments,
            capital_bound,
            risk_bound,
            expiration,
            scope,
        ) = _validated_terms(
            self.bundle_hash,
            self.environment,
            self.action,
            self.instruments,
            self.capital_bound,
            self.risk_bound,
            self.expiration,
            self.scope,
        )
        proposed_at = _aware_datetime(self.proposed_at, "proposed_at")
        if expiration <= proposed_at:
            raise ConsentError("expiration must be after the proposal time")
        if scope.kind is GrantScopeKind.INTERVAL:
            if scope.require_interval_end() > expiration:
                raise ConsentError("interval scope must end at or before expiration")
        object.__setattr__(self, "bundle_hash", bundle_hash)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "instruments", instruments)
        object.__setattr__(self, "capital_bound", capital_bound)
        object.__setattr__(self, "risk_bound", risk_bound)
        object.__setattr__(self, "expiration", expiration)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "proposed_at", proposed_at)

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this proposal."""
        return {
            "action": self.action.value,
            "bundleHash": self.bundle_hash,
            "capitalBound": _decimal_text(self.capital_bound),
            "environment": self.environment.value,
            "expiration": _timestamp_text(self.expiration),
            "instruments": list(self.instruments),
            "proposedAt": _timestamp_text(self.proposed_at),
            "riskBound": _decimal_text(self.risk_bound),
            "riskBoundKind": RISK_BOUND_KIND,
            "schema": self.schema,
            "scope": self.scope.canonical_document,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


@dataclass(frozen=True, slots=True)
class ConsentConfirmation:
    """The explicit structured restatement that creates a grant.

    The confirming surface (a structured chat reply or the UI/API) must
    restate the exact bundle hash, the typed environment and action, and
    carry an operator-issued confirmation token.  Free-text agreement is
    structurally impossible: no field accepts it, and every field here is
    validated against a strict pattern or enum.
    """

    bundle_hash: str
    environment: GrantEnvironment
    action: GrantAction
    confirmation_token: str
    schema: str = _SCHEMA_CONFIRMATION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "bundle_hash", _digest(self.bundle_hash, "bundle_hash")
        )
        if not isinstance(self.environment, GrantEnvironment):
            raise ConsentError(
                "environment must be a GrantEnvironment; text cannot confirm activation"
            )
        if not isinstance(self.action, GrantAction):
            raise ConsentError(
                "action must be a GrantAction; text cannot confirm activation"
            )
        if (
            not isinstance(self.confirmation_token, str)
            or _TOKEN.fullmatch(self.confirmation_token) is None
        ):
            raise ConsentError(
                "confirmation_token must be an operator-issued confirmation token"
            )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this confirmation."""
        return {
            "action": self.action.value,
            "bundleHash": self.bundle_hash,
            "confirmationToken": self.confirmation_token,
            "environment": self.environment.value,
            "schema": self.schema,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    """A scoped, revocable, expiring consent to activate one strategy bundle.

    ``risk_bound`` is the grant's max-daily-loss bound: the maximum daily
    loss the gatekeeper must enforce for activations made under this grant;
    it is the only risk limit this grant model carries.  ``capital_bound``
    is the exact capital ceiling and ``instruments`` are the only permitted
    symbols.  The grant is active strictly before its ``expiration``
    instant and, for interval scope, strictly before the interval end.
    """

    bundle_hash: str
    environment: GrantEnvironment
    action: GrantAction
    instruments: tuple[str, ...]
    capital_bound: Decimal
    risk_bound: Decimal
    expiration: datetime
    scope: GrantScope
    created_at: datetime
    schema: str = _SCHEMA_GRANT

    def __post_init__(self) -> None:
        (
            bundle_hash,
            environment,
            action,
            instruments,
            capital_bound,
            risk_bound,
            expiration,
            scope,
        ) = _validated_terms(
            self.bundle_hash,
            self.environment,
            self.action,
            self.instruments,
            self.capital_bound,
            self.risk_bound,
            self.expiration,
            self.scope,
        )
        created_at = _aware_datetime(self.created_at, "created_at")
        if expiration <= created_at:
            raise ConsentError("expiration must be after grant creation")
        if scope.kind is GrantScopeKind.INTERVAL:
            interval_end = scope.require_interval_end()
            if interval_end <= created_at:
                raise ConsentError("interval scope must end after grant creation")
            if interval_end > expiration:
                raise ConsentError("interval scope must end at or before expiration")
        object.__setattr__(self, "bundle_hash", bundle_hash)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "instruments", instruments)
        object.__setattr__(self, "capital_bound", capital_bound)
        object.__setattr__(self, "risk_bound", risk_bound)
        object.__setattr__(self, "expiration", expiration)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "created_at", created_at)

    @property
    def grant_id(self) -> str:
        """Return the domain-separated content address of this grant."""
        return hashlib.sha256(
            _GRANT_HASH_DOMAIN
            + _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document the grant hash covers."""
        return {
            "action": self.action.value,
            "bundleHash": self.bundle_hash,
            "capitalBound": _decimal_text(self.capital_bound),
            "createdAt": _timestamp_text(self.created_at),
            "environment": self.environment.value,
            "expiration": _timestamp_text(self.expiration),
            "instruments": list(self.instruments),
            "riskBound": _decimal_text(self.risk_bound),
            "riskBoundKind": RISK_BOUND_KIND,
            "schema": self.schema,
            "scope": self.scope.canonical_document,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


@dataclass(frozen=True, slots=True)
class RevokedGrant:
    """The audit record that a grant was revoked and must not activate."""

    grant_id: str
    bundle_hash: str
    environment: GrantEnvironment
    action: GrantAction
    revoked_at: datetime
    reason: str
    schema: str = _SCHEMA_REVOCATION

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _digest(self.grant_id, "grant_id"))
        object.__setattr__(
            self, "bundle_hash", _digest(self.bundle_hash, "bundle_hash")
        )
        if not isinstance(self.environment, GrantEnvironment):
            raise ConsentError("environment must be a GrantEnvironment")
        if not isinstance(self.action, GrantAction):
            raise ConsentError("action must be a GrantAction")
        object.__setattr__(
            self, "revoked_at", _aware_datetime(self.revoked_at, "revoked_at")
        )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ConsentError("reason must be a non-empty explanation")
        object.__setattr__(self, "reason", self.reason.strip())

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this revocation."""
        return {
            "action": self.action.value,
            "bundleHash": self.bundle_hash,
            "environment": self.environment.value,
            "grantId": self.grant_id,
            "reason": self.reason,
            "revokedAt": _timestamp_text(self.revoked_at),
            "schema": self.schema,
        }

    @property
    def canonical_json(self) -> str:
        """Return the deterministic canonical JSON text for audit records."""
        return _canonical_json(self.canonical_document)


def propose_grant(
    *,
    bundle_hash: str,
    environment: GrantEnvironment,
    action: GrantAction,
    instruments: tuple[str, ...],
    capital_bound: Decimal,
    risk_bound: Decimal,
    expiration: datetime,
    scope: GrantScope,
    proposed_at: datetime,
) -> ConsentProposal:
    """Build a consent proposal from typed structured input only.

    There is deliberately no text-bearing parameter: natural-language
    agreement can never produce a proposal, only an explicit translation
    into these typed fields can.

    Raises:
        ConsentError: if any term is missing, mistyped, or inconsistent.
    """
    return ConsentProposal(
        bundle_hash=bundle_hash,
        environment=environment,
        action=action,
        instruments=instruments,
        capital_bound=capital_bound,
        risk_bound=risk_bound,
        expiration=expiration,
        scope=scope,
        proposed_at=proposed_at,
    )


def confirm_grant(
    proposal: ConsentProposal,
    confirmation: ConsentConfirmation,
    *,
    created_at: datetime,
) -> ConsentGrant:
    """Create a grant only from an exact structured restatement.

    The confirmation must restate the proposal's bundle hash, environment,
    and action exactly; the remaining terms are inherited from the proposal.
    Free-text agreement is structurally impossible because the only accepted
    confirmation type is :class:`ConsentConfirmation`.

    Raises:
        ConsentError: if the restatement differs or the times are
            inconsistent with the proposal.
    """
    if not isinstance(proposal, ConsentProposal):
        raise ConsentError("proposal must be a ConsentProposal")
    if not isinstance(confirmation, ConsentConfirmation):
        raise ConsentError(
            "confirmation must be a ConsentConfirmation; free-text agreement"
            " cannot confirm a grant"
        )
    created = _aware_datetime(created_at, "created_at")
    if confirmation.bundle_hash != proposal.bundle_hash:
        raise ConsentError("confirmation bundle_hash does not restate the proposal")
    if confirmation.environment is not proposal.environment:
        raise ConsentError("confirmation environment does not restate the proposal")
    if confirmation.action is not proposal.action:
        raise ConsentError("confirmation action does not restate the proposal")
    if proposal.expiration <= created:
        raise ConsentError("grant expiration must be after creation")
    if proposal.scope.kind is GrantScopeKind.INTERVAL:
        if proposal.scope.require_interval_end() <= created:
            raise ConsentError("interval scope must end after grant creation")
    return ConsentGrant(
        bundle_hash=proposal.bundle_hash,
        environment=proposal.environment,
        action=proposal.action,
        instruments=proposal.instruments,
        capital_bound=proposal.capital_bound,
        risk_bound=proposal.risk_bound,
        expiration=proposal.expiration,
        scope=proposal.scope,
        created_at=created,
    )


def revoke(grant: ConsentGrant, *, at: datetime, reason: str) -> RevokedGrant:
    """Revoke a grant and return the audit record of the revocation.

    Raises:
        ConsentError: if the grant is not a :class:`ConsentGrant`, the time
            is naive or precedes grant creation, or the reason is blank.
    """
    if not isinstance(grant, ConsentGrant):
        raise ConsentError("grant must be a ConsentGrant")
    revoked_at = _aware_datetime(at, "at")
    if not isinstance(reason, str) or not reason.strip():
        raise ConsentError("reason must be a non-empty explanation")
    if revoked_at < grant.created_at:
        raise ConsentError("revocation time must not precede grant creation")
    return RevokedGrant(
        grant_id=grant.grant_id,
        bundle_hash=grant.bundle_hash,
        environment=grant.environment,
        action=grant.action,
        revoked_at=revoked_at,
        reason=reason,
    )


def is_active(
    grant: ConsentGrant,
    at: datetime,
    *,
    revocations: Collection[RevokedGrant] = (),
) -> bool:
    """Return whether ``grant`` is usable at ``at``.

    The grant is active only strictly before its expiration instant and,
    for interval scope, strictly before its interval end; a grant whose
    revocation record appears in ``revocations`` is never active.  Time
    inputs must be timezone-aware.  Activation checks must combine this
    with the revocation ledger (see ``thesis.activation.requests``), which
    is the authoritative revocation and consumption view.
    """
    checked_at = _aware_datetime(at, "at")
    if any(record.grant_id == grant.grant_id for record in revocations):
        return False
    if checked_at >= grant.expiration:
        return False
    if grant.scope.kind is GrantScopeKind.INTERVAL:
        if checked_at >= grant.scope.require_interval_end():
            return False
    return True


__all__ = [
    "ConsentConfirmation",
    "ConsentError",
    "ConsentGrant",
    "ConsentProposal",
    "DEFAULT_ACTIONS_DISABLED",
    "GrantAction",
    "GrantEnvironment",
    "GrantScope",
    "GrantScopeKind",
    "RISK_BOUND_KIND",
    "RevokedGrant",
    "confirm_grant",
    "interval_scope",
    "is_active",
    "one_shot_scope",
    "propose_grant",
    "revoke",
]
