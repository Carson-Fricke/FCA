"""Invite lifecycle application service for Thesis.

The service owns invite creation, listing, and pre-redemption revocation over
an injected :class:`~thesis.identity.ports.InviteStore` and a caller-supplied
clock.  It is the only place that ever sees a raw invite token: creation
generates a URL-safe 256-bit token, persists only the domain-separated
:func:`~thesis.identity.models.invite_token_hash` digest, and returns the raw
token plus the one-time signup link exactly once.  Nothing here reads the wall
clock, logs a token, or stores a raw token.

Delivery is a structural seam.  The optional :class:`InviteMailer` protocol is
defined locally so the concrete Azure Communication Services mailer (T310) can
implement it without this module importing ``thesis.identity.mailer``.  When a
mailer is supplied and the invite is bound to an email address, the invitation
is delivered before it is persisted: a delivery failure raises
:class:`InviteDeliveryError` with an actionable, token-free message and leaves
the store untouched, so no unreachable invite is written.  (The reverse race —
a successful send followed by a failed write — is left to the store's own
durability guarantees and is deliberately not masked here.)
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from thesis.identity.models import (
    AccountRole,
    Identifier,
    Invite,
    Note,
    invite_token_hash,
    normalize_email,
)
from thesis.identity.ports import InviteStore

#: A caller-supplied source of timezone-aware instants; the service never reads
#: the wall clock itself.
Clock = Callable[[], datetime]


def _require_aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True, slots=True)
class IssuedInvite:
    """A freshly created invite and its one-time raw link material.

    ``token`` and ``invite_link`` are excluded from ``repr`` so a stray log or
    traceback never records the raw credential.  Callers deliver the link and
    then discard both; neither is recoverable from the store.
    """

    invite: Invite
    token: str = field(repr=False)
    invite_link: str = field(repr=False)


@runtime_checkable
class InviteMailer(Protocol):
    """Structural delivery seam for invitation emails.

    The concrete mailer (T310) implements this without the identity service
    depending on a provider module.  An implementation must raise on failure so
    :meth:`InviteService.create_invite` can surface a token-free
    :class:`InviteDeliveryError`.
    """

    def send_invite(
        self, *, to: str, invite_link: str, expires_at: datetime
    ) -> None: ...


@runtime_checkable
class InviteDirectory(InviteStore, Protocol):
    """An :class:`InviteStore` that can also enumerate its records.

    ``InviteStore`` itself is deliberately write/consume only; adapters that
    can enumerate satisfy this extension so the service can implement
    ``list_invites`` without widening the base port.
    """

    def list(self) -> tuple[Invite, ...]: ...


class InviteError(RuntimeError):
    """Base class for invite service failures."""


class InviteDeliveryError(InviteError):
    """An invitation could not be delivered to the invitee.

    The message never contains the raw token or the signup link.
    """


class InviteListingUnavailableError(InviteError):
    """The injected store cannot enumerate invites."""


class InviteService:
    """Create, list, and revoke single-use Thesis invites.

    Args:
        store: Durable invite store implementing
            :class:`~thesis.identity.ports.InviteStore`.  ``list_invites``
            additionally requires the store to implement ``list``
            (:class:`InviteDirectory`).
        clock: Caller-supplied source of timezone-aware timestamps.
        mailer: Optional delivery seam.  When absent, creation returns the
            one-time link to the caller for out-of-band delivery.
    """

    def __init__(
        self,
        store: InviteStore,
        *,
        clock: Clock,
        mailer: InviteMailer | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._mailer = mailer

    def _now(self) -> datetime:
        return _require_aware(self._clock(), "clock")

    def create_invite(
        self,
        *,
        email: str | None = None,
        role: AccountRole = "member",
        expires_in: timedelta,
        note: Note | None = None,
        created_by: Identifier,
        invite_link_base: str,
    ) -> IssuedInvite:
        """Create and (when configured) deliver one single-use invite.

        The raw token is generated here, hashed immediately, and returned only
        in the result; the persisted :class:`Invite` carries the digest alone.
        When a mailer is supplied and ``email`` is set the invitation is sent
        before the record is written, so a delivery failure leaves the store
        unchanged and raises a token-free :class:`InviteDeliveryError`.
        """
        if not isinstance(expires_in, timedelta) or expires_in <= timedelta(0):
            raise ValueError("expires_in must be a positive timedelta")
        base = invite_link_base.strip()
        if not base:
            raise ValueError("invite_link_base must be a non-empty URL")
        bound_email = None if email is None else normalize_email(email)
        now = self._now()

        raw_token = secrets.token_urlsafe(32)
        invite = Invite(
            id=f"inv-{secrets.token_hex(16)}",
            token_hash=invite_token_hash(raw_token),
            bound_email=bound_email,
            role=role,
            expires_at=now + expires_in,
            created_by=created_by,
            created_at=now,
            note=note,
        )
        invite_link = f"{base}?token={raw_token}"

        mailer = self._mailer
        if mailer is not None and bound_email is not None:
            try:
                mailer.send_invite(
                    to=bound_email,
                    invite_link=invite_link,
                    expires_at=invite.expires_at,
                )
            except Exception as error:
                raise InviteDeliveryError(
                    f"could not deliver the invitation for {bound_email!r}"
                ) from error

        persisted = self._store.put(invite)
        return IssuedInvite(invite=persisted, token=raw_token, invite_link=invite_link)

    def list_invites(self) -> tuple[Invite, ...]:
        """Return every stored invite; no raw token exists on a record."""
        if not isinstance(self._store, InviteDirectory):
            raise InviteListingUnavailableError(
                "the injected InviteStore cannot list invites; the store must "
                "also implement list()"
            )
        return tuple(self._store.list())

    def revoke_invite(self, invite_id: Identifier, *, at: datetime) -> bool:
        """Revoke an unconsumed invite at ``at``; return whether it changed.

        Returns ``False`` when the invite does not exist, was already revoked,
        or was already redeemed, so a consumed invite can never be revoked.
        """
        checked_at = _require_aware(at, "at")
        return self._store.revoke(invite_id, at=checked_at) is not None


__all__ = [
    "Clock",
    "InviteDeliveryError",
    "InviteDirectory",
    "InviteError",
    "InviteListingUnavailableError",
    "InviteMailer",
    "InviteService",
    "IssuedInvite",
]
