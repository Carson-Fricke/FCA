"""Unified identity audit trail, residency adapter, and rate limiting.

This module is the public-auth hardening seam for Thesis.  It provides:

* :class:`IdentityAuditEvent` — one immutable, secret-free audit record for a
  login, logout, invite lifecycle, or consent outcome.
* :class:`IdentityAuditSink` — the structural ``record(event)`` collector that
  endpoints and services depend on; :class:`RecordingIdentityAuditSink` is a
  deterministic in-memory implementation for tests and local development.
* :class:`IdentityAuditTrail` — the ergonomic emitter that stamps events from
  an injected clock, so no call site reads the wall clock.
* :class:`RedemptionIdentityAuditSink` — adapts T311's
  :class:`~thesis.identity.redemption.RedemptionAuditEvent` onto the unified
  event so redemption reuses the same sink.
* :class:`ResidencyIdentityAuditSink` — maps a unified event onto the
  workspace-resident
  :class:`~thesis.agent.residency.OperationAuditRecord` document for durable
  storage through the same ``append_audit`` port the agent uses.
* :class:`FixedWindowRateLimiter` and :class:`IdentityRateLimiters` — the
  injectable login/invite/redemption attempt budgets.  The limiter implements
  T311's structural ``RedemptionRateLimiter`` and is consulted with a
  caller-supplied instant, never a wall clock.

Secret discipline
-----------------

An :class:`IdentityAuditEvent` deliberately has no field for a raw token, a
signup link, a session id, a CSRF secret, a credential, or any other secret.
Callers record only non-secret identifiers (account, actor, invite, email,
client key) and a short reason.  Tests assert the property directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from thesis.identity.models import normalize_email
from thesis.identity.redemption import RedemptionAuditEvent

if TYPE_CHECKING:
    from thesis.agent.residency import OperationAuditRecord, OperationKind

#: A caller-supplied source of timezone-aware instants; no function here reads
#: the wall clock.
Clock = Callable[[], datetime]

#: The identity lifecycle act recorded by one audit event.
IdentityAuditKind = Literal[
    "login",
    "logout",
    "invite_created",
    "invite_revoked",
    "invite_redeemed",
    "consent",
]

#: The terminal outcome recorded for one identity act.
IdentityAuditOutcome = Literal["success", "failure", "rate_limited"]

#: The rate-limit buckets an identity surface consults, in policy order.
IdentityRateLimitKind = Literal["login", "invite", "redemption"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_DIGEST_DOMAIN = b"fca:thesis-identity-audit:v1\0"


def _require_aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _identifier(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a portable identifier")
    return value


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _new_audit_id() -> str:
    return f"audit-{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class IdentityAuditEvent:
    """One immutable, secret-free identity audit record.

    Only non-secret identifiers are carried.  ``reason`` is required for a
    non-success outcome and forbidden for a success, mirroring the residency
    audit record's coherence rule.
    """

    kind: IdentityAuditKind
    at: datetime
    outcome: IdentityAuditOutcome = "success"
    actor_id: str | None = None
    account_id: str | None = None
    invite_id: str | None = None
    email: str | None = None
    reason: str | None = None
    client_key: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in (
            "login",
            "logout",
            "invite_created",
            "invite_revoked",
            "invite_redeemed",
            "consent",
        ):
            raise ValueError(f"unsupported identity audit kind: {self.kind!r}")
        if self.outcome not in ("success", "failure", "rate_limited"):
            raise ValueError(f"unsupported identity audit outcome: {self.outcome!r}")
        object.__setattr__(self, "at", _require_aware(self.at, "at"))
        _identifier(self.actor_id, "actor_id")
        _identifier(self.account_id, "account_id")
        _identifier(self.invite_id, "invite_id")
        if self.email is not None:
            object.__setattr__(self, "email", normalize_email(self.email))
        if self.client_key is not None and (
            not isinstance(self.client_key, str) or not self.client_key.strip()
        ):
            raise ValueError("client_key must be non-empty text when provided")
        if self.outcome == "success":
            if self.reason is not None:
                raise ValueError("a successful identity audit carries no reason")
        elif not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a non-success identity audit requires a reason")

    def as_dict(self) -> dict[str, object]:
        """Return the canonical JSON values, never a secret or raw credential."""
        return {
            "accountId": self.account_id,
            "actorId": self.actor_id,
            "at": _iso(self.at),
            "clientKey": self.client_key,
            "email": self.email,
            "inviteId": self.invite_id,
            "kind": self.kind,
            "outcome": self.outcome,
            "reason": self.reason,
        }

    @property
    def digest(self) -> str:
        """Return a domain-separated digest of the non-secret event values."""
        payload = _canonical_json(self.as_dict()).encode("utf-8")
        return hashlib.sha256(_DIGEST_DOMAIN + payload).hexdigest()


@runtime_checkable
class IdentityAuditSink(Protocol):
    """Structural collector for unified identity audit events."""

    def record(self, event: IdentityAuditEvent) -> None: ...


class RecordingIdentityAuditSink:
    """A deterministic in-memory :class:`IdentityAuditSink` for tests/dev."""

    def __init__(self) -> None:
        self._events: list[IdentityAuditEvent] = []

    @property
    def events(self) -> tuple[IdentityAuditEvent, ...]:
        """Return the recorded events in insertion order."""
        return tuple(self._events)

    def record(self, event: IdentityAuditEvent) -> None:
        self._events.append(event)

    def drain(self) -> tuple[IdentityAuditEvent, ...]:
        """Return and clear the recorded events."""
        drained = tuple(self._events)
        self._events.clear()
        return drained

    def clear(self) -> None:
        """Discard every recorded event."""
        self._events.clear()


class IdentityAuditTrail:
    """Emit unified identity audit events using an injected clock.

    The trail is the single integration point for login, logout, invite, and
    consent call sites: each method stamps the event from the injected clock and
    forwards it to the sink, so no call site reads the wall clock or assembles a
    record by hand.
    """

    def __init__(self, sink: IdentityAuditSink, *, clock: Clock) -> None:
        self._sink = sink
        self._clock = clock

    def _now(self) -> datetime:
        return _require_aware(self._clock(), "clock")

    def _emit(
        self,
        kind: IdentityAuditKind,
        *,
        outcome: IdentityAuditOutcome = "success",
        actor_id: str | None = None,
        account_id: str | None = None,
        invite_id: str | None = None,
        email: str | None = None,
        reason: str | None = None,
        client_key: str | None = None,
        at: datetime | None = None,
    ) -> IdentityAuditEvent:
        event = IdentityAuditEvent(
            kind=kind,
            at=self._now() if at is None else _require_aware(at, "at"),
            outcome=outcome,
            actor_id=actor_id,
            account_id=account_id,
            invite_id=invite_id,
            email=email,
            reason=reason,
            client_key=client_key,
        )
        self._sink.record(event)
        return event

    def login(
        self,
        *,
        account_id: str,
        actor_id: str | None = None,
        outcome: IdentityAuditOutcome = "success",
        reason: str | None = None,
        client_key: str | None = None,
    ) -> IdentityAuditEvent:
        """Record one login attempt."""
        return self._emit(
            "login",
            outcome=outcome,
            actor_id=actor_id,
            account_id=account_id,
            reason=reason,
            client_key=client_key,
        )

    def logout(
        self,
        *,
        account_id: str,
        actor_id: str | None = None,
        outcome: IdentityAuditOutcome = "success",
        reason: str | None = None,
    ) -> IdentityAuditEvent:
        """Record one logout (server-side session revocation)."""
        return self._emit(
            "logout",
            outcome=outcome,
            actor_id=actor_id,
            account_id=account_id,
            reason=reason,
        )

    def invite_created(
        self,
        *,
        invite_id: str,
        actor_id: str,
        email: str | None = None,
        outcome: IdentityAuditOutcome = "success",
        reason: str | None = None,
        client_key: str | None = None,
    ) -> IdentityAuditEvent:
        """Record one invite creation; never the raw token or signup link."""
        return self._emit(
            "invite_created",
            outcome=outcome,
            actor_id=actor_id,
            invite_id=invite_id,
            email=email,
            reason=reason,
            client_key=client_key,
        )

    def invite_revoked(
        self,
        *,
        invite_id: str,
        actor_id: str,
        outcome: IdentityAuditOutcome = "success",
        reason: str | None = None,
    ) -> IdentityAuditEvent:
        """Record one pre-redemption invite revocation."""
        return self._emit(
            "invite_revoked",
            outcome=outcome,
            actor_id=actor_id,
            invite_id=invite_id,
            reason=reason,
        )

    def invite_redeemed(
        self,
        *,
        invite_id: str | None,
        email: str | None = None,
        account_id: str | None = None,
        outcome: IdentityAuditOutcome = "success",
        reason: str | None = None,
        client_key: str | None = None,
        at: datetime | None = None,
    ) -> IdentityAuditEvent:
        """Record one redemption outcome; never the raw token or signup link."""
        return self._emit(
            "invite_redeemed",
            outcome=outcome,
            account_id=account_id,
            invite_id=invite_id,
            email=email,
            reason=reason,
            client_key=client_key,
            at=at,
        )

    def consent(
        self,
        *,
        account_id: str,
        actor_id: str | None = None,
        outcome: IdentityAuditOutcome = "success",
        reason: str | None = None,
    ) -> IdentityAuditEvent:
        """Record one consent grant decision."""
        return self._emit(
            "consent",
            outcome=outcome,
            actor_id=actor_id,
            account_id=account_id,
            reason=reason,
        )


class RedemptionIdentityAuditSink:
    """Adapt T311 redemption audit events onto the unified identity sink.

    ``RedemptionService`` records a :class:`RedemptionAuditEvent`; this adapter
    translates it to a secret-free :class:`IdentityAuditEvent` and forwards it,
    so redemption shares the same audit trail as login, invite, and consent
    without importing the HTTP layer.
    """

    def __init__(self, sink: IdentityAuditSink) -> None:
        self._sink = sink

    def record(self, event: RedemptionAuditEvent) -> None:
        if event.outcome == "redeemed":
            outcome: IdentityAuditOutcome = "success"
            reason = None
        elif event.reason == "rate_limited":
            outcome = "rate_limited"
            reason = event.reason
        else:
            outcome = "failure"
            reason = event.reason or "redemption_rejected"
        self._sink.record(
            IdentityAuditEvent(
                kind="invite_redeemed",
                at=event.at,
                outcome=outcome,
                invite_id=event.invite_id,
                email=event.email,
                reason=reason,
                client_key=event.client_key,
            )
        )


def access_audit_operation(event: IdentityAuditEvent) -> OperationKind:
    """Map an identity event onto the residency access-audit operation.

    The residency :data:`~thesis.agent.residency.OperationKind` taxonomy is a
    closed set of agent operations; identity access events are recorded under
    the read-only ``open_investigation`` carrier and the precise identity kind
    is preserved in the record's ``route_ref`` (``identity/<kind>``).  A caller
    may inject its own mapping when the durable store has a dedicated identity
    taxonomy.
    """
    del event
    return "open_investigation"


@runtime_checkable
class OperationAuditWriter(Protocol):
    """The durable append seam the residency store already implements."""

    def append_audit(self, record: OperationAuditRecord) -> object: ...


class ResidencyIdentityAuditSink:
    """Map unified identity events onto durable residency audit records.

    Each event becomes one
    :class:`~thesis.agent.residency.OperationAuditRecord` appended through the
    injected writer (structurally the residency store's ``append_audit``).  The
    record carries the non-secret identifiers and a domain-separated digest of
    the event values; it never carries a token, link, or secret.
    """

    def __init__(
        self,
        writer: OperationAuditWriter,
        *,
        workspace_id: str,
        session_id: str,
        clock: Clock,
        operation: OperationKind = "open_investigation",
        operation_id_factory: Callable[[], str] = _new_audit_id,
    ) -> None:
        self._writer = writer
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._clock = clock
        self._operation = operation
        self._operation_id_factory = operation_id_factory

    def record(self, event: IdentityAuditEvent) -> None:
        from thesis.agent.residency import OperationAuditRecord

        recorded_at = _require_aware(self._clock(), "clock")
        successful = event.outcome == "success"
        record = OperationAuditRecord(
            operation_id=self._operation_id_factory(),
            workspace_id=self._workspace_id,
            operation=self._operation,
            status="recorded" if successful else "unavailable",
            inputs_digest=event.digest,
            created_at=event.at,
            recorded_at=recorded_at,
            session_id=self._session_id,
            reason=None if successful else (event.reason or "identity_audit_failure"),
            route_ref=f"identity/{event.kind}",
            detail_ref=event.invite_id or event.account_id or event.actor_id,
        )
        self._writer.append_audit(record)


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """One fixed-window attempt budget."""

    attempts: int
    window: timedelta

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("rate-limit attempts must be at least one")
        if self.window <= timedelta(0):
            raise ValueError("rate-limit window must be positive")


class FixedWindowRateLimiter:
    """An injectable fixed-window limiter over caller-supplied instants.

    ``allow(key, at=...)`` returns ``True`` while fewer than ``policy.attempts``
    attempts have been observed for ``key`` inside the current window and
    ``False`` once the budget is exhausted.  The window rolls on the first
    attempt at or after ``window_start + policy.window``.  It satisfies T311's
    structural :class:`~thesis.identity.redemption.RedemptionRateLimiter` and
    never reads a wall clock.
    """

    def __init__(self, policy: RateLimitPolicy | None = None) -> None:
        self._policy = (
            RateLimitPolicy(attempts=5, window=timedelta(minutes=1))
            if policy is None
            else policy
        )
        self._windows: dict[str, tuple[datetime, int]] = {}

    @property
    def policy(self) -> RateLimitPolicy:
        """Return the configured attempt budget."""
        return self._policy

    def allow(self, key: str, *, at: datetime) -> bool:
        """Record one attempt for ``key`` and return whether it is permitted."""
        moment = _require_aware(at, "at")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("rate-limit key must be non-empty text")
        start, count = self._windows.get(key, (moment, 0))
        if moment >= start + self._policy.window:
            start, count = moment, 0
        if count >= self._policy.attempts:
            self._windows[key] = (start, count)
            return False
        self._windows[key] = (start, count + 1)
        return True

    def reset(self, key: str | None = None) -> None:
        """Clear one key's window, or every window when ``key`` is ``None``."""
        if key is None:
            self._windows.clear()
        else:
            self._windows.pop(key, None)


@dataclass(frozen=True, slots=True)
class IdentityRateLimiters:
    """The login, invite, and redemption attempt budgets as one policy set."""

    login: FixedWindowRateLimiter
    invite: FixedWindowRateLimiter
    redemption: FixedWindowRateLimiter

    def limiter_for(self, kind: IdentityRateLimitKind) -> FixedWindowRateLimiter:
        """Return the limiter responsible for ``kind``."""
        if kind == "login":
            return self.login
        if kind == "invite":
            return self.invite
        return self.redemption

    @classmethod
    def from_policies(
        cls,
        *,
        login: RateLimitPolicy,
        invite: RateLimitPolicy,
        redemption: RateLimitPolicy,
    ) -> IdentityRateLimiters:
        """Build one limiter per surface from explicit policies."""
        return cls(
            login=FixedWindowRateLimiter(login),
            invite=FixedWindowRateLimiter(invite),
            redemption=FixedWindowRateLimiter(redemption),
        )


__all__ = [
    "Clock",
    "FixedWindowRateLimiter",
    "IdentityAuditEvent",
    "IdentityAuditKind",
    "IdentityAuditOutcome",
    "IdentityAuditSink",
    "IdentityAuditTrail",
    "IdentityRateLimitKind",
    "IdentityRateLimiters",
    "OperationAuditWriter",
    "RateLimitPolicy",
    "RecordingIdentityAuditSink",
    "RedemptionIdentityAuditSink",
    "ResidencyIdentityAuditSink",
    "access_audit_operation",
]
