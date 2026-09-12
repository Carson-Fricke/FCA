"""Durable account, invite, and session records for Thesis identity.

This module holds the storage-independent domain records for app-owned
accounts, one-time invitations, and server-side sessions.  It imports no
storage provider: the Azure Table and local JSON/SQLite adapters live in
separate modules and depend on these records, never the reverse.

Invariants enforced here:

* Every record is frozen and strictly validated; undeclared fields and
  implicit type coercions are rejected.
* Timestamps are always supplied by the caller.  No function in this module
  reads the wall clock, so adapters and tests stay deterministic.
* Secret material is never persisted in raw form.  An invite stores only the
  domain-separated SHA-256 ``token_hash`` of the single-use link token; the
  raw token exists only in the invitation delivered to the user and is never
  accepted by this module or by any store built on these records.  Session
  records are retrieved by an opaque identifier rather than by the browser
  credential, and both the identifier and the CSRF secret are excluded from
  the record ``repr``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

#: Domain separation prevents an invite token hash from colliding with any
#: other SHA-256 value computed elsewhere in the platform.
_INVITE_TOKEN_DOMAIN = b"fca:thesis-invite-token:v1\0"
_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
Subject = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[\x21-\x7e]+$",
    ),
]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_048)]
CsrfSecret = Annotated[str, StringConstraints(min_length=8, max_length=256)]

AccountRole = Literal["owner", "admin", "member"]
AccountStatus = Literal["active", "disabled"]


class StrictIdentityModel(BaseModel):
    """Base model that rejects undeclared fields and implicit coercion."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


def normalize_email(value: object) -> str:
    """Return a trimmed, lowercase email address or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("email must be a string")
    candidate = value.strip().lower()
    if len(candidate) > 254 or _EMAIL_PATTERN.fullmatch(candidate) is None:
        raise ValueError("email must be a valid address")
    return candidate


def _email_before(value: object) -> object:
    if isinstance(value, str):
        return normalize_email(value)
    return value


Email = Annotated[str, BeforeValidator(_email_before)]


def _utc_datetime(value: object) -> object:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("must be an ISO 8601 datetime") from error
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a UTC offset")
        return value.astimezone(UTC)
    return value


UtcDateTime = Annotated[datetime, BeforeValidator(_utc_datetime)]


def _require_aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def invite_token_hash(raw_token: str) -> str:
    """Return the domain-separated SHA-256 hash persisted for an invite token.

    Callers hash the raw token immediately and keep it only long enough to
    deliver the invitation link.  The raw token is never stored in an
    :class:`Invite` record or in a store that persists one.
    """
    if not isinstance(raw_token, str) or not raw_token.strip():
        raise ValueError("invite token must be a non-empty string")
    digest = hashlib.sha256(_INVITE_TOKEN_DOMAIN + raw_token.encode("utf-8"))
    return digest.hexdigest()


class OAuthSubject(StrictIdentityModel):
    """One external identity provider subject linked to a local account."""

    provider: Literal["entra", "google", "github"]
    subject: Subject


class Account(StrictIdentityModel):
    """A durable app-owned account with a unique normalized email address."""

    id: Identifier
    email: Email
    display_name: DisplayName
    oauth_subjects: tuple[OAuthSubject, ...] = ()
    role: AccountRole
    status: AccountStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _account_is_coherent(self) -> Account:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        pairs = [(link.provider, link.subject) for link in self.oauth_subjects]
        if len(pairs) != len(set(pairs)):
            raise ValueError("oauth_subjects must be unique per provider")
        return self


class Invite(StrictIdentityModel):
    """A single-use, expiring invitation that can create one account.

    Only the SHA-256 ``token_hash`` is persisted; see
    :func:`invite_token_hash`.  An invite is redeemable only before
    ``expires_at`` and only while it is neither consumed nor revoked.  When
    ``bound_email`` is set, redemption is restricted to that normalized
    address.
    """

    id: Identifier
    token_hash: Sha256Digest = Field(repr=False)
    bound_email: Email | None = None
    role: AccountRole
    expires_at: UtcDateTime
    consumed_at: UtcDateTime | None = None
    revoked_at: UtcDateTime | None = None
    created_by: Identifier
    created_at: UtcDateTime
    note: Note | None = None

    @model_validator(mode="after")
    def _invite_lifecycle_is_ordered(self) -> Invite:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        events = (("consumed_at", self.consumed_at), ("revoked_at", self.revoked_at))
        for name, value in events:
            if value is not None and value < self.created_at:
                raise ValueError(f"{name} must not precede created_at")
        if self.consumed_at is not None and self.consumed_at >= self.expires_at:
            raise ValueError("consumed_at must be before expires_at")
        if self.consumed_at is not None and self.revoked_at is not None:
            raise ValueError("an invite cannot be both consumed and revoked")
        return self

    def is_redeemable(self, at: datetime) -> bool:
        """Return whether this invite can be redeemed once at ``at``."""
        checked_at = _require_aware(at, "at")
        return (
            self.consumed_at is None
            and self.revoked_at is None
            and self.created_at <= checked_at < self.expires_at
        )

    def is_bound_to(self, email: str) -> bool:
        """Return whether ``email`` may redeem this invite.

        An invite without a bound email accepts any valid address.
        """
        candidate = normalize_email(email)
        return self.bound_email is None or candidate == self.bound_email


class Session(StrictIdentityModel):
    """A durable server-side session referencing one account by identity.

    The record is keyed by an opaque ``id``; the browser credential that
    resolves to it is never stored here.  ``csrf_secret`` is server-side
    secret material and is excluded from the record ``repr``.
    """

    id: Identifier = Field(repr=False)
    account_id: Identifier
    created_at: UtcDateTime
    expires_at: UtcDateTime
    revoked_at: UtcDateTime | None = None
    csrf_secret: CsrfSecret = Field(repr=False)

    @model_validator(mode="after")
    def _session_lifecycle_is_ordered(self) -> Session:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at must not precede created_at")
        return self

    def is_active(self, at: datetime) -> bool:
        """Return whether the session is usable at ``at``."""
        checked_at = _require_aware(at, "at")
        return (
            self.revoked_at is None and self.created_at <= checked_at < self.expires_at
        )


__all__ = [
    "Account",
    "AccountRole",
    "AccountStatus",
    "CsrfSecret",
    "DisplayName",
    "Email",
    "Identifier",
    "Invite",
    "Note",
    "OAuthSubject",
    "Session",
    "Sha256Digest",
    "StrictIdentityModel",
    "Subject",
    "UtcDateTime",
    "invite_token_hash",
    "normalize_email",
]
