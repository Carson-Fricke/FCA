"""Azure Table adapters for the Thesis identity repository ports.

The three stores here implement :mod:`thesis.identity.ports` against an
injected :class:`azure.data.tables.TableClient`.  No connection string,
account name, or credential is read or hard-coded in this module: callers own
the client and supply it, so configuration and Key Vault access stay outside
the storage boundary.

Partition and row-key scheme
----------------------------

Azure Tables has no schema and no secondary indexes, so each entity kind gets
its own partition and every lookup value that must be unique gets a small
index row.  Keys are URL-encoded with the same convention used by
``thesis.workspaces.azure_contract`` (``urllib.parse.quote(..., safe="._-~")``)
so an identity column never contains a partition separator.

============== ========================= ===============================
Partition      Row key                   Meaning
============== ========================= ===============================
``accounts``   ``quote(account.id)``     One :class:`Account` per row.
``account-emails`` ``quote(account.email)`` Email -> ``AccountId`` unique index.
``invites``    ``quote(invite.id)``      One :class:`Invite` per row.
``invite-tokens`` ``token_hash``          Token hash -> ``InviteId`` index.
``sessions``   ``quote(session.id)``     One :class:`Session` per row.
============== ========================= ===============================

Serialization is explicit and deterministic.  Timestamps are stored as
ISO-8601 strings with a UTC offset, optional fields are omitted rather than
written as ``null``, and the OAuth subject list is one canonical JSON string.
Timestamps are always supplied by the caller; nothing here reads the wall
clock, matching the no-wall-clock invariant in :mod:`thesis.identity.models`.

Atomic single-use semantics
---------------------------

``InviteStore.consume`` and ``InviteStore.revoke`` are compare-and-set
transitions.  The adapter reads the entity, validates every precondition in
the caller's record (token hash, consumed/revoked flags, redeemable window,
bound email), then writes with the entity's ETag and ``IfNotModified``.  The
service rejects the write with ``ResourceModifiedError`` when a concurrent
redemption already changed the row, so exactly one caller observes the
post-consumption receipt and every loser receives ``None``.
``SessionStore.revoke`` uses the same ETag pattern.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from thesis.identity.models import (
    Account,
    Invite,
    OAuthSubject,
    Session,
    normalize_email,
)

_ACCOUNTS_PARTITION: Final = "accounts"
_ACCOUNT_EMAILS_PARTITION: Final = "account-emails"
_INVITES_PARTITION: Final = "invites"
_INVITE_TOKENS_PARTITION: Final = "invite-tokens"
_SESSIONS_PARTITION: Final = "sessions"


def _table_key(value: str) -> str:
    """Encode a value for use as an Azure Table partition or row key."""
    return quote(value, safe="._-~")


def _utc_iso(value: datetime) -> str:
    """Render a timezone-aware datetime as a canonical UTC ISO-8601 string."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _as_utc(value: datetime, name: str) -> datetime:
    """Normalize a caller-supplied instant to UTC, rejecting naive values."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _utc_timestamp(entity: Mapping[str, object], name: str) -> datetime:
    """Read a required UTC timestamp written by :func:`_utc_iso`."""
    raw = entity[name]
    if isinstance(raw, datetime):
        parsed = raw
    else:
        parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return parsed.astimezone(UTC)


def _optional_utc_timestamp(entity: Mapping[str, object], name: str) -> datetime | None:
    """Read an optional UTC timestamp, treating an absent field as ``None``."""
    if entity.get(name) is None:
        return None
    return _utc_timestamp(entity, name)


def _optional_string(entity: Mapping[str, object], name: str) -> str | None:
    value = entity.get(name)
    return None if value is None else str(value)


def _oauth_subjects_to_json(subjects: tuple[OAuthSubject, ...]) -> str:
    """Serialize OAuth subjects into one canonical, sort-stable JSON column."""
    return json.dumps(
        [{"provider": link.provider, "subject": link.subject} for link in subjects],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _oauth_subjects_from_json(raw: object) -> tuple[OAuthSubject, ...]:
    if raw is None:
        return ()
    decoded: object = json.loads(str(raw))
    if not isinstance(decoded, list):
        raise TypeError("OAuthSubjects must decode to a JSON array")
    return tuple(OAuthSubject.model_validate(item) for item in decoded)


def account_to_entity(account: Account) -> TableEntity:
    """Map an account to its Azure Table-compatible entity."""
    entity = TableEntity()
    entity.update(
        {
            "PartitionKey": _ACCOUNTS_PARTITION,
            "RowKey": _table_key(account.id),
            "AccountId": account.id,
            "Email": account.email,
            "DisplayName": account.display_name,
            "OAuthSubjects": _oauth_subjects_to_json(account.oauth_subjects),
            "Role": account.role,
            "Status": account.status,
            "CreatedAt": _utc_iso(account.created_at),
            "UpdatedAt": _utc_iso(account.updated_at),
        }
    )
    return entity


def account_from_entity(entity: Mapping[str, object]) -> Account:
    """Restore an account from an Azure Table entity."""
    return Account.model_validate(
        {
            "id": entity["AccountId"],
            "email": entity["Email"],
            "display_name": entity["DisplayName"],
            "oauth_subjects": _oauth_subjects_from_json(entity.get("OAuthSubjects")),
            "role": entity["Role"],
            "status": entity["Status"],
            "created_at": _utc_timestamp(entity, "CreatedAt"),
            "updated_at": _utc_timestamp(entity, "UpdatedAt"),
        }
    )


def invite_to_entity(invite: Invite) -> TableEntity:
    """Map an invite to its Azure Table-compatible entity."""
    entity = TableEntity()
    entity.update(
        {
            "PartitionKey": _INVITES_PARTITION,
            "RowKey": _table_key(invite.id),
            "InviteId": invite.id,
            "TokenHash": invite.token_hash,
            "Role": invite.role,
            "ExpiresAt": _utc_iso(invite.expires_at),
            "CreatedBy": invite.created_by,
            "CreatedAt": _utc_iso(invite.created_at),
        }
    )
    if invite.bound_email is not None:
        entity["BoundEmail"] = invite.bound_email
    if invite.consumed_at is not None:
        entity["ConsumedAt"] = _utc_iso(invite.consumed_at)
    if invite.revoked_at is not None:
        entity["RevokedAt"] = _utc_iso(invite.revoked_at)
    if invite.note is not None:
        entity["Note"] = invite.note
    return entity


def invite_from_entity(entity: Mapping[str, object]) -> Invite:
    """Restore an invite from an Azure Table entity."""
    return Invite.model_validate(
        {
            "id": entity["InviteId"],
            "token_hash": entity["TokenHash"],
            "bound_email": _optional_string(entity, "BoundEmail"),
            "role": entity["Role"],
            "expires_at": _utc_timestamp(entity, "ExpiresAt"),
            "consumed_at": _optional_utc_timestamp(entity, "ConsumedAt"),
            "revoked_at": _optional_utc_timestamp(entity, "RevokedAt"),
            "created_by": entity["CreatedBy"],
            "created_at": _utc_timestamp(entity, "CreatedAt"),
            "note": _optional_string(entity, "Note"),
        }
    )


def session_to_entity(session: Session) -> TableEntity:
    """Map a session to its Azure Table-compatible entity."""
    entity = TableEntity()
    entity.update(
        {
            "PartitionKey": _SESSIONS_PARTITION,
            "RowKey": _table_key(session.id),
            "SessionId": session.id,
            "AccountId": session.account_id,
            "CreatedAt": _utc_iso(session.created_at),
            "ExpiresAt": _utc_iso(session.expires_at),
            "CsrfSecret": session.csrf_secret,
        }
    )
    if session.revoked_at is not None:
        entity["RevokedAt"] = _utc_iso(session.revoked_at)
    return entity


def session_from_entity(entity: Mapping[str, object]) -> Session:
    """Restore a session from an Azure Table entity."""
    return Session.model_validate(
        {
            "id": entity["SessionId"],
            "account_id": entity["AccountId"],
            "created_at": _utc_timestamp(entity, "CreatedAt"),
            "expires_at": _utc_timestamp(entity, "ExpiresAt"),
            "revoked_at": _optional_utc_timestamp(entity, "RevokedAt"),
            "csrf_secret": entity["CsrfSecret"],
        }
    )


def _account_email_index_entity(account: Account) -> TableEntity:
    entity = TableEntity()
    entity.update(
        {
            "PartitionKey": _ACCOUNT_EMAILS_PARTITION,
            "RowKey": _table_key(account.email),
            "AccountId": account.id,
        }
    )
    return entity


def _invite_token_index_entity(invite: Invite) -> TableEntity:
    entity = TableEntity()
    entity.update(
        {
            "PartitionKey": _INVITE_TOKENS_PARTITION,
            "RowKey": _table_key(invite.token_hash),
            "InviteId": invite.id,
        }
    )
    return entity


class _TableAccess:
    """Shared ETag compare-and-set helper for the three stores."""

    def __init__(self, table: TableClient) -> None:
        self._table = table

    def _get(self, partition_key: str, row_key: str) -> TableEntity | None:
        try:
            return self._table.get_entity(partition_key, row_key)
        except ResourceNotFoundError:
            return None

    def _replace_if_unchanged(
        self,
        current: TableEntity,
        entity: Mapping[str, object],
    ) -> bool:
        """Replace ``current`` only when its ETag is still the stored one.

        Returns ``True`` when this caller won the transition and ``False`` when
        a concurrent writer already changed (or removed) the entity.
        """
        etag = current.metadata.get("etag")
        try:
            self._table.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceModifiedError, ResourceNotFoundError):
            return False
        return True


class AzureTableAccountStore(_TableAccess):
    """Account directory backed by an Azure Tables ``TableClient``."""

    def get(self, account_id: str) -> Account | None:
        entity = self._get(_ACCOUNTS_PARTITION, _table_key(account_id))
        if entity is None:
            return None
        return account_from_entity(entity)

    def get_by_email(self, email: str) -> Account | None:
        index = self._get(_ACCOUNT_EMAILS_PARTITION, _table_key(normalize_email(email)))
        if index is None:
            return None
        return self.get(str(index["AccountId"]))

    def put(self, account: Account) -> Account:
        owner = self._get(_ACCOUNT_EMAILS_PARTITION, _table_key(account.email))
        if owner is not None and str(owner["AccountId"]) != account.id:
            raise ValueError("email already belongs to another account")
        existing = self.get(account.id)
        if existing is not None and existing.email != account.email:
            self._table.delete_entity(
                _ACCOUNT_EMAILS_PARTITION, _table_key(existing.email)
            )
        self._table.upsert_entity(account_to_entity(account), mode=UpdateMode.REPLACE)
        self._table.upsert_entity(
            _account_email_index_entity(account), mode=UpdateMode.REPLACE
        )
        return account

    def list(self) -> tuple[Account, ...]:
        accounts = (
            account_from_entity(entity)
            for entity in self._table.query_entities(
                "PartitionKey eq @partition",
                parameters={"partition": _ACCOUNTS_PARTITION},
            )
        )
        return tuple(sorted(accounts, key=lambda account: account.id))


class AzureTableInviteStore(_TableAccess):
    """Invite store backed by an Azure Tables ``TableClient``.

    ``consume`` and ``revoke`` are atomic compare-and-set transitions guarded
    by the entity ETag, so concurrent redemptions cannot both succeed.
    """

    def get(self, invite_id: str) -> Invite | None:
        entity = self._get(_INVITES_PARTITION, _table_key(invite_id))
        if entity is None:
            return None
        return invite_from_entity(entity)

    def get_by_token_hash(self, token_hash: str) -> Invite | None:
        index = self._get(_INVITE_TOKENS_PARTITION, _table_key(token_hash))
        if index is None:
            return None
        return self.get(str(index["InviteId"]))

    def put(self, invite: Invite) -> Invite:
        owner = self._get(_INVITE_TOKENS_PARTITION, _table_key(invite.token_hash))
        if owner is not None and str(owner["InviteId"]) != invite.id:
            raise ValueError("token hash already belongs to another invite")
        self._table.upsert_entity(invite_to_entity(invite), mode=UpdateMode.REPLACE)
        self._table.upsert_entity(
            _invite_token_index_entity(invite), mode=UpdateMode.REPLACE
        )
        return invite

    def consume(
        self,
        invite_id: str,
        *,
        token_hash: str,
        at: datetime,
        email: str | None = None,
    ) -> Invite | None:
        at = _as_utc(at, "at")
        current = self._get(_INVITES_PARTITION, _table_key(invite_id))
        if current is None:
            return None
        invite = invite_from_entity(current)
        if invite.token_hash != token_hash:
            return None
        if not invite.is_redeemable(at):
            return None
        if email is not None and not invite.is_bound_to(email):
            return None
        redeemed = invite.model_copy(update={"consumed_at": at})
        if not self._replace_if_unchanged(current, invite_to_entity(redeemed)):
            return None
        return redeemed

    def revoke(self, invite_id: str, *, at: datetime) -> Invite | None:
        at = _as_utc(at, "at")
        current = self._get(_INVITES_PARTITION, _table_key(invite_id))
        if current is None:
            return None
        invite = invite_from_entity(current)
        if invite.consumed_at is not None or invite.revoked_at is not None:
            return None
        if at < invite.created_at:
            return None
        revoked = invite.model_copy(update={"revoked_at": at})
        if not self._replace_if_unchanged(current, invite_to_entity(revoked)):
            return None
        return revoked


class AzureTableSessionStore(_TableAccess):
    """Session store backed by an Azure Tables ``TableClient``."""

    def get(self, session_id: str) -> Session | None:
        entity = self._get(_SESSIONS_PARTITION, _table_key(session_id))
        if entity is None:
            return None
        return session_from_entity(entity)

    def put(self, session: Session) -> Session:
        self._table.upsert_entity(session_to_entity(session), mode=UpdateMode.REPLACE)
        return session

    def revoke(self, session_id: str, *, at: datetime) -> Session | None:
        at = _as_utc(at, "at")
        current = self._get(_SESSIONS_PARTITION, _table_key(session_id))
        if current is None:
            return None
        session = session_from_entity(current)
        if not session.is_active(at):
            return None
        revoked = session.model_copy(update={"revoked_at": at})
        if not self._replace_if_unchanged(current, session_to_entity(revoked)):
            return None
        return revoked

    def delete_expired(self, *, at: datetime) -> int:
        at = _as_utc(at, "at")
        removed = 0
        for entity in self._table.query_entities(
            "PartitionKey eq @partition",
            parameters={"partition": _SESSIONS_PARTITION},
        ):
            session = session_from_entity(entity)
            if at >= session.expires_at:
                self._table.delete_entity(_SESSIONS_PARTITION, _table_key(session.id))
                removed += 1
        return removed


__all__ = [
    "AzureTableAccountStore",
    "AzureTableInviteStore",
    "AzureTableSessionStore",
    "account_from_entity",
    "account_to_entity",
    "invite_from_entity",
    "invite_to_entity",
    "session_from_entity",
    "session_to_entity",
]
