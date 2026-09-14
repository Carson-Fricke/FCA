"""Seed the default Thesis owner and claim the default workspace.

The public Thesis deployment has exactly one seeded owner account, derived
from the ``THESIS_OWNER_EMAIL`` environment variable.  This module owns that
bootstrap and the mapping that lets the seeded account act as the default
workspace owner (``local-operator``) in
:mod:`thesis.auth.workspace_access`.

Design invariants:

* Seeding is idempotent.  The account id is derived deterministically from the
  normalized owner email, so re-seeding returns the same account and never
  creates a duplicate.  An existing account for the email is reused and, when
  warranted, promoted to ``role="owner"``/``status="active"``.
* No wall clock is read.  Every timestamp comes from the injected ``clock``.
* No secret is read, logged, or persisted.  The owner email is personal data,
  not a credential; it is never written to a log or an error message.
* The ownership claim is a pure, deterministic mapping.  Repeated logins
  rebuild the same :class:`OwnerClaim` and never re-create an account, so the
  first matching login claims ownership exactly once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from thesis.identity.models import Account, Identifier, normalize_email
from thesis.identity.ports import AccountStore

if TYPE_CHECKING:
    from thesis.auth.workspace_access import AccountOwnerMap

#: A caller-supplied source of timezone-aware instants; this module never reads
#: the wall clock itself.
Clock = Callable[[], datetime]

#: Environment variable that carries the seeded owner's email address.
OWNER_EMAIL_ENV_VAR = "THESIS_OWNER_EMAIL"

#: The default workspace's ``owner.owner_id`` in ``config/thesis/workspace.yaml``.
DEFAULT_WORKSPACE_OWNER_ID = "local-operator"

_OWNER_ACCOUNT_ID_PREFIX = "owner-"


class OwnerConfigurationError(ValueError):
    """The owner seeding configuration is missing or invalid."""


def _require_aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _normalize_owner_email(email: str) -> str:
    try:
        return normalize_email(email)
    except ValueError as error:
        raise OwnerConfigurationError("owner email must be a valid address") from error


def derive_owner_account_id(email: str) -> Identifier:
    """Return the deterministic account id for the normalized owner email.

    Deriving the id from the email keeps re-seeding stable: the same owner
    email always maps to the same account id, so a second seed call reuses the
    existing account instead of creating a duplicate.
    """
    normalized = _normalize_owner_email(email)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_OWNER_ACCOUNT_ID_PREFIX}{digest[:32]}"


def seed_owner_account(
    account_store: AccountStore,
    *,
    email: str,
    display_name: str | None = None,
    clock: Clock,
) -> Account:
    """Seed the owner account for ``email`` idempotently.

    When an account already exists for the normalized email it is returned
    unchanged if it is already an active owner; otherwise it is promoted in
    place (same id and creation time) to ``role="owner"``/``status="active"``.
    When no account exists, one is created with a deterministic id derived
    from the email, so re-seeding is stable and never duplicates the account.

    Raises:
        OwnerConfigurationError: when ``email`` is not a valid address.
    """
    normalized = _normalize_owner_email(email)
    now = _require_aware(clock(), "clock")
    existing = account_store.get_by_email(normalized)
    if existing is not None:
        if existing.role == "owner" and existing.status == "active":
            return existing
        promoted = Account(
            id=existing.id,
            email=existing.email,
            display_name=existing.display_name,
            oauth_subjects=existing.oauth_subjects,
            role="owner",
            status="active",
            created_at=existing.created_at,
            updated_at=now,
        )
        return account_store.put(promoted)
    name = display_name if display_name is not None else normalized.split("@", 1)[0]
    account = Account(
        id=derive_owner_account_id(normalized),
        email=normalized,
        display_name=name,
        role="owner",
        status="active",
        created_at=now,
        updated_at=now,
    )
    return account_store.put(account)


def seed_owner_from_environment(
    account_store: AccountStore,
    *,
    environ: Mapping[str, str],
    clock: Clock,
) -> Account | None:
    """Seed the owner from ``THESIS_OWNER_EMAIL`` when it is configured.

    Returns the seeded (or reused) owner account, or ``None`` when the variable
    is unset or blank.  The unset case is deliberately a no-op rather than an
    error: local development and tests run without an owner, and the login and
    authorization paths already fail closed for any account that is not the
    seeded owner.  The email value is never logged and never appears in an
    error message.
    """
    raw = environ.get(OWNER_EMAIL_ENV_VAR)
    if raw is None or not raw.strip():
        return None
    return seed_owner_account(account_store, email=raw, clock=clock)


@dataclass(frozen=True, slots=True)
class OwnerClaim:
    """The seeded owner account mapped onto the default workspace owner.

    The claim is derived deterministically from the seeded account id, so
    rebuilding it on every login is idempotent: it never creates an account and
    repeated calls yield an equal mapping.
    """

    owner_account_id: Identifier
    workspace_owner_id: Identifier = DEFAULT_WORKSPACE_OWNER_ID

    def account_owner_map(self) -> AccountOwnerMap:
        """Return the ``workspace_access`` account-to-owner mapping."""
        return owner_account_map(
            self.owner_account_id, workspace_owner_id=self.workspace_owner_id
        )


def owner_account_map(
    owner_account_id: Identifier,
    *,
    workspace_owner_id: Identifier = DEFAULT_WORKSPACE_OWNER_ID,
) -> AccountOwnerMap:
    """Return an :data:`~thesis.auth.workspace_access.AccountOwnerMap`.

    The returned callable maps the seeded owner account id to
    ``workspace_owner_id`` (the default workspace's ``owner.owner_id``) and
    every other account id to itself, preserving the default
    :func:`~thesis.auth.workspace_access.identity_account_owner` behavior for
    non-owner accounts.  It is a pure function of its inputs, so repeated
    matching logins rebuild the same mapping and claim ownership exactly once.

    Raises:
        OwnerConfigurationError: when either id is empty.
    """
    if not owner_account_id:
        raise OwnerConfigurationError("owner account id must be non-empty")
    if not workspace_owner_id:
        raise OwnerConfigurationError("workspace owner id must be non-empty")

    def account_owner(account_id: str) -> str:
        if account_id == owner_account_id:
            return workspace_owner_id
        return account_id

    return account_owner


def claim_owner(
    account_store: AccountStore,
    *,
    email: str,
    workspace_owner_id: Identifier = DEFAULT_WORKSPACE_OWNER_ID,
    clock: Clock,
) -> OwnerClaim:
    """Seed the owner for ``email`` and return its workspace ownership claim.

    This is the login-path seam: the first sign-in whose email matches the
    seeded owner seeds (or reuses) the account and receives the mapping that
    lets it act as the default workspace owner.  Repeated calls are idempotent
    and never create a second account.
    """
    account = seed_owner_account(account_store, email=email, clock=clock)
    return OwnerClaim(
        owner_account_id=account.id, workspace_owner_id=workspace_owner_id
    )


__all__ = [
    "Clock",
    "DEFAULT_WORKSPACE_OWNER_ID",
    "OWNER_EMAIL_ENV_VAR",
    "OwnerClaim",
    "OwnerConfigurationError",
    "claim_owner",
    "derive_owner_account_id",
    "owner_account_map",
    "seed_owner_account",
    "seed_owner_from_environment",
]
