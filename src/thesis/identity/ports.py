"""Storage-agnostic repository ports for accounts, invites, and sessions.

The Azure Table and local JSON/SQLite adapters implement these
runtime-checkable protocols.  No storage provider is imported here, so domain
and application code depend on the ports alone.  All time inputs are supplied
by the caller, mirroring the records in :mod:`thesis.identity.models`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from thesis.identity.models import (
    Account,
    Email,
    Identifier,
    Invite,
    Session,
    Sha256Digest,
)


@runtime_checkable
class AccountStore(Protocol):
    """Durable account directory keyed by id with a unique email index."""

    def get(self, account_id: Identifier) -> Account | None:
        """Return the account with ``account_id``, or ``None``."""

    def get_by_email(self, email: Email) -> Account | None:
        """Return the account whose normalized email matches, or ``None``."""

    def put(self, account: Account) -> Account:
        """Insert or replace ``account`` by id, keeping email addresses unique."""

    def list(self) -> tuple[Account, ...]:
        """Return every stored account in a deterministic order."""


@runtime_checkable
class InviteStore(Protocol):
    """Durable invite store with an atomic single-use consume contract."""

    def get(self, invite_id: Identifier) -> Invite | None:
        """Return the invite with ``invite_id``, or ``None``."""

    def get_by_token_hash(self, token_hash: Sha256Digest) -> Invite | None:
        """Return the invite matching the hashed token, or ``None``."""

    def put(self, invite: Invite) -> Invite:
        """Insert or replace ``invite`` by id."""

    def consume(
        self,
        invite_id: Identifier,
        *,
        token_hash: Sha256Digest,
        at: datetime,
        email: Email | None = None,
    ) -> Invite | None:
        """Atomically redeem a single-use invite.

        Compare-and-set: the transition succeeds only when the stored invite
        still carries this ``token_hash``, is neither consumed nor revoked,
        is redeemable at ``at``, and — when ``email`` is supplied — is bound
        to that address.  Concurrent callers race for exactly one winning
        transition; every loser receives ``None``.  A non-``None`` result is
        the post-consumption receipt (``consumed_at`` set).
        """

    def revoke(self, invite_id: Identifier, *, at: datetime) -> Invite | None:
        """Atomically revoke an unconsumed invite, returning it or ``None``."""


@runtime_checkable
class SessionStore(Protocol):
    """Durable server-side session store supporting explicit revocation."""

    def get(self, session_id: Identifier) -> Session | None:
        """Return the session with ``session_id``, or ``None``."""

    def put(self, session: Session) -> Session:
        """Insert or replace ``session`` by id."""

    def revoke(self, session_id: Identifier, *, at: datetime) -> Session | None:
        """Atomically mark an active session revoked, returning it or ``None``."""

    def delete_expired(self, *, at: datetime) -> int:
        """Delete sessions expired at ``at`` and return the number removed."""


__all__ = [
    "AccountStore",
    "InviteStore",
    "SessionStore",
]
