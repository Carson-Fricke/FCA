"""One-time invite redemption with atomic consume, binding, and audit.

:class:`RedemptionService` is the only place that turns a delivered invite
token into an app-owned account.  It composes the storage-agnostic
:class:`~thesis.identity.ports.InviteStore` and
:class:`~thesis.identity.ports.AccountStore` ports with a caller-supplied
clock, an optional rate limiter, and an optional audit sink.  Nothing here
reads the wall clock, opens a network connection, or embeds a secret.

Redemption is fail-closed and single-use:

1. The raw token is hashed immediately with
   :func:`~thesis.identity.models.invite_token_hash`; only the digest is ever
   compared or used as a rate-limit key.  The raw token never reaches a store,
   an audit event, or an error message.
2. The rate limiter is consulted first, keyed by the caller's ``client_key``
   when present and otherwise by the token digest.
3. The invite is looked up by digest and rejected when it is unknown, expired,
   revoked, already consumed, or bound to a different email.
4. The invite is consumed through the store's compare-and-set
   :meth:`~thesis.identity.ports.InviteStore.consume`; exactly one concurrent
   caller observes the post-consumption receipt and every loser is rejected.
5. The account is provisioned for the invite's normalized email with the
   invite's role and any linked OAuth subjects.

Existing-account policy
-----------------------

Provisioning is deliberately fail-closed: when an account already exists for
the invite's email, redemption is rejected with :class:`InvalidInviteError`
rather than silently adopting the existing account.  Adopting would let an
invite mutate an account it was not issued for (for example by attaching OAuth
subjects or changing the role), so the service refuses instead.  The check runs
before the atomic consume, so a rejected redemption leaves the invite
redeemable; the account store's unique-email constraint remains the backstop
for the narrow race between the check and the write.

Audit
-----

Every terminal outcome — success, rejection, and rate limiting — is reported
to the injected :class:`RedemptionAuditSink`.  The default sink is a no-op, so
callers opt in to auditing.  A :class:`RedemptionAuditEvent` carries the
outcome, the injected timestamp, the invite id (when known), the normalized
email, a short reason, and the caller's client key; it never carries the raw
token, the signup link, or any secret.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NoReturn, Protocol, runtime_checkable

from thesis.identity.models import (
    Account,
    Identifier,
    Invite,
    OAuthSubject,
    invite_token_hash,
    normalize_email,
)
from thesis.identity.ports import AccountStore, InviteStore

#: A caller-supplied source of timezone-aware instants; the service never reads
#: the wall clock itself.
Clock = Callable[[], datetime]

#: The terminal outcome recorded for one redemption attempt.
RedemptionOutcome = Literal["redeemed", "rejected"]


def _require_aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


class RedemptionError(RuntimeError):
    """Base class for invite redemption failures."""


class InvalidInviteError(RedemptionError):
    """The invite is unknown, expired, revoked, consumed, or already bound.

    The message never contains the raw token or the signup link.
    """


class InviteEmailMismatchError(RedemptionError):
    """The supplied email does not match the invite's bound email."""


class RedemptionRateLimited(RedemptionError):
    """The caller exceeded the configured redemption attempt budget."""


@runtime_checkable
class RedemptionRateLimiter(Protocol):
    """Structural rate-limit seam consulted before any store access.

    ``allow`` returns ``True`` when the attempt identified by ``key`` may
    proceed at ``at`` and ``False`` when the budget is exhausted.  The key is
    derived from the caller's client key and/or the token digest, never from
    the raw token.
    """

    def allow(self, key: str, *, at: datetime) -> bool: ...


@dataclass(frozen=True, slots=True)
class RedemptionAuditEvent:
    """One immutable audit record for a redemption attempt.

    The record deliberately omits the raw token, the signup link, and every
    secret; ``invite_id`` is ``None`` when the token did not resolve to an
    invite.
    """

    outcome: RedemptionOutcome
    at: datetime
    invite_id: Identifier | None
    email: str | None
    reason: str | None = None
    client_key: str | None = None


@runtime_checkable
class RedemptionAuditSink(Protocol):
    """Structural audit seam for redemption outcomes."""

    def record(self, event: RedemptionAuditEvent) -> None: ...


class _NullAuditSink:
    """Default audit sink that discards every event."""

    def record(self, event: RedemptionAuditEvent) -> None:
        return None


_NULL_AUDIT = _NullAuditSink()


@dataclass(frozen=True, slots=True)
class RedemptionResult:
    """The account created by a successful redemption and its receipt."""

    account: Account
    invite: Invite


class RedemptionService:
    """Redeem single-use invites into app-owned accounts.

    Args:
        invite_store: Durable invite store implementing the atomic
            :class:`~thesis.identity.ports.InviteStore` consume contract.
        account_store: Durable account directory implementing
            :class:`~thesis.identity.ports.AccountStore`.
        clock: Caller-supplied source of timezone-aware timestamps.
        audit: Optional audit sink; defaults to a no-op.
        rate_limiter: Optional rate limiter consulted before any store access.
    """

    def __init__(
        self,
        invite_store: InviteStore,
        account_store: AccountStore,
        clock: Clock,
        *,
        audit: RedemptionAuditSink | None = None,
        rate_limiter: RedemptionRateLimiter | None = None,
    ) -> None:
        self._invites = invite_store
        self._accounts = account_store
        self._clock = clock
        self._audit: RedemptionAuditSink = _NULL_AUDIT if audit is None else audit
        self._rate_limiter = rate_limiter

    def _now(self) -> datetime:
        return _require_aware(self._clock(), "clock")

    def _emit(
        self,
        *,
        outcome: RedemptionOutcome,
        at: datetime,
        invite_id: Identifier | None,
        email: str | None,
        reason: str | None,
        client_key: str | None,
    ) -> None:
        self._audit.record(
            RedemptionAuditEvent(
                outcome=outcome,
                at=at,
                invite_id=invite_id,
                email=email,
                reason=reason,
                client_key=client_key,
            )
        )

    def _reject(
        self,
        *,
        at: datetime,
        invite_id: Identifier | None,
        email: str | None,
        reason: str,
        client_key: str | None,
        error: RedemptionError,
    ) -> NoReturn:
        self._emit(
            outcome="rejected",
            at=at,
            invite_id=invite_id,
            email=email,
            reason=reason,
            client_key=client_key,
        )
        raise error

    @staticmethod
    def _rate_key(client_key: str | None, token_hash: str) -> str:
        if client_key is not None and client_key.strip():
            return f"client:{client_key.strip()}"
        return f"token:{token_hash}"

    def redeem(
        self,
        *,
        token: str,
        email: str,
        display_name: str | None = None,
        oauth_subjects: tuple[OAuthSubject, ...] = (),
        client_key: str | None = None,
    ) -> RedemptionResult:
        """Redeem ``token`` for ``email`` exactly once.

        Raises:
            RedemptionRateLimited: when the injected limiter denies the
                attempt.
            InviteEmailMismatchError: when the invite is bound to a different
                email.
            InvalidInviteError: when the invite is unknown, expired, revoked,
                already consumed, lost a concurrent consume race, or an
                account already exists for the email.
        """
        now = self._now()
        normalized_email = normalize_email(email)
        token_hash = invite_token_hash(token)

        limiter = self._rate_limiter
        if limiter is not None and not limiter.allow(
            self._rate_key(client_key, token_hash), at=now
        ):
            self._reject(
                at=now,
                invite_id=None,
                email=normalized_email,
                reason="rate_limited",
                client_key=client_key,
                error=RedemptionRateLimited("too many redemption attempts"),
            )

        invite = self._invites.get_by_token_hash(token_hash)
        if invite is None:
            self._reject(
                at=now,
                invite_id=None,
                email=normalized_email,
                reason="unknown_token",
                client_key=client_key,
                error=InvalidInviteError("invite token is not recognized"),
            )
        if invite.revoked_at is not None:
            self._reject(
                at=now,
                invite_id=invite.id,
                email=normalized_email,
                reason="revoked",
                client_key=client_key,
                error=InvalidInviteError("invite has been revoked"),
            )
        if invite.consumed_at is not None:
            self._reject(
                at=now,
                invite_id=invite.id,
                email=normalized_email,
                reason="consumed",
                client_key=client_key,
                error=InvalidInviteError("invite has already been redeemed"),
            )
        if not invite.is_redeemable(now):
            self._reject(
                at=now,
                invite_id=invite.id,
                email=normalized_email,
                reason="expired",
                client_key=client_key,
                error=InvalidInviteError("invite is expired"),
            )
        if not invite.is_bound_to(normalized_email):
            self._reject(
                at=now,
                invite_id=invite.id,
                email=normalized_email,
                reason="email_mismatch",
                client_key=client_key,
                error=InviteEmailMismatchError(
                    "invite is bound to a different email address"
                ),
            )
        if self._accounts.get_by_email(normalized_email) is not None:
            self._reject(
                at=now,
                invite_id=invite.id,
                email=normalized_email,
                reason="account_exists",
                client_key=client_key,
                error=InvalidInviteError(
                    "an account already exists for this email address"
                ),
            )

        receipt = self._invites.consume(
            invite.id,
            token_hash=token_hash,
            at=now,
            email=normalized_email,
        )
        if receipt is None:
            self._reject(
                at=now,
                invite_id=invite.id,
                email=normalized_email,
                reason="consume_race",
                client_key=client_key,
                error=InvalidInviteError("invite is no longer redeemable"),
            )

        account = self._provision(
            receipt,
            email=normalized_email,
            display_name=display_name,
            oauth_subjects=oauth_subjects,
            at=now,
        )
        self._emit(
            outcome="redeemed",
            at=now,
            invite_id=receipt.id,
            email=normalized_email,
            reason=None,
            client_key=client_key,
        )
        return RedemptionResult(account=account, invite=receipt)

    def _provision(
        self,
        invite: Invite,
        *,
        email: str,
        display_name: str | None,
        oauth_subjects: tuple[OAuthSubject, ...],
        at: datetime,
    ) -> Account:
        name = display_name if display_name is not None else email.split("@", 1)[0]
        account = Account(
            id=f"acct-{secrets.token_hex(16)}",
            email=email,
            display_name=name,
            oauth_subjects=tuple(oauth_subjects),
            role=invite.role,
            status="active",
            created_at=at,
            updated_at=at,
        )
        return self._accounts.put(account)


__all__ = [
    "Clock",
    "InvalidInviteError",
    "InviteEmailMismatchError",
    "RedemptionAuditEvent",
    "RedemptionAuditSink",
    "RedemptionError",
    "RedemptionOutcome",
    "RedemptionRateLimited",
    "RedemptionRateLimiter",
    "RedemptionResult",
    "RedemptionService",
]
