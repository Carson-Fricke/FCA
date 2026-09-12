"""Offline tests for the Azure Table identity stores.

Every test injects a fake ``TableClient`` that mimics the SDK's entity and
ETag semantics, so nothing here talks to Azure.  Timestamps are fixed and
passed in, keeping the suite deterministic and wall-clock-free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from thesis.identity.azure import (
    AzureTableAccountStore,
    AzureTableInviteStore,
    AzureTableSessionStore,
    account_from_entity,
    account_to_entity,
    invite_from_entity,
    invite_to_entity,
    session_from_entity,
    session_to_entity,
)
from thesis.identity.models import (
    Account,
    Invite,
    OAuthSubject,
    Session,
    invite_token_hash,
)
from thesis.identity.ports import AccountStore, InviteStore, SessionStore

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_RAW_TOKEN = "raw-invite-token-1"
_TOKEN_HASH = invite_token_hash(_RAW_TOKEN)


class FakeTable:
    """Minimal in-memory stand-in for ``azure.data.tables.TableClient``."""

    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], TableEntity] = {}
        self.etags: dict[tuple[str, str], str] = {}
        self.queries: list[tuple[str, dict[str, object] | None]] = []
        self.deletes: list[tuple[str, str]] = []
        self.updates: list[tuple[str, str]] = []
        self._counter = 0

    def _next_etag(self) -> str:
        self._counter += 1
        return f'W/"etag-{self._counter}"'

    def get_entity(self, partition_key: str, row_key: str) -> TableEntity:
        key = (partition_key, row_key)
        try:
            stored = self.entities[key]
        except KeyError:
            raise ResourceNotFoundError("missing") from None
        entity = TableEntity(stored)
        entity._metadata = {"etag": self.etags[key], "timestamp": None}
        return entity

    def upsert_entity(
        self,
        entity: TableEntity,
        mode: UpdateMode = UpdateMode.MERGE,
        **kwargs: Any,
    ) -> None:
        assert mode is UpdateMode.REPLACE
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        self.entities[key] = TableEntity(entity)
        self.etags[key] = self._next_etag()

    def update_entity(
        self,
        entity: TableEntity,
        mode: UpdateMode = UpdateMode.MERGE,
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
        **kwargs: Any,
    ) -> None:
        assert mode is UpdateMode.REPLACE
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key not in self.entities:
            raise ResourceNotFoundError("missing")
        if match_condition is MatchConditions.IfNotModified and self.etags[key] != etag:
            raise ResourceModifiedError("etag mismatch")
        self.entities[key] = TableEntity(entity)
        self.etags[key] = self._next_etag()
        self.updates.append(key)

    def query_entities(
        self,
        query_filter: str,
        *,
        parameters: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> list[TableEntity]:
        self.queries.append((query_filter, parameters))
        partition = (parameters or {}).get("partition")
        return [
            self.get_entity(partition_key, row_key)
            for partition_key, row_key in list(self.entities)
            if partition_key == partition
        ]

    def delete_entity(self, partition_key: str, row_key: str, **kwargs: Any) -> None:
        self.deletes.append((partition_key, row_key))
        self.entities.pop((partition_key, row_key), None)
        self.etags.pop((partition_key, row_key), None)


class RacingTable(FakeTable):
    """Changes the stored ETag before the first conditional update lands."""

    def __init__(self) -> None:
        super().__init__()
        self.race = True

    def update_entity(
        self,
        entity: TableEntity,
        mode: UpdateMode = UpdateMode.MERGE,
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
        **kwargs: Any,
    ) -> None:
        if self.race:
            self.race = False
            for key in list(self.etags):
                self.etags[key] = "changed-by-concurrent-writer"
        super().update_entity(
            entity, mode, etag=etag, match_condition=match_condition, **kwargs
        )


def _account(
    account_id: str = "acct-1",
    email: str = "Owner@Example.COM",
    role: str = "owner",
) -> Account:
    return Account(
        id=account_id,
        email=email,
        display_name="Owner One",
        oauth_subjects=(OAuthSubject(provider="github", subject="gh-1"),),
        role=cast(Any, role),
        status="active",
        created_at=_T0,
        updated_at=_T0,
    )


def _invite(
    invite_id: str = "inv-1",
    *,
    bound_email: str | None = None,
    expires_at: datetime = _T0 + timedelta(hours=1),
) -> Invite:
    return Invite(
        id=invite_id,
        token_hash=_TOKEN_HASH,
        bound_email=bound_email,
        role="member",
        expires_at=expires_at,
        created_by="acct-1",
        created_at=_T0,
    )


def _session(
    session_id: str = "sess-1",
    *,
    expires_at: datetime = _T0 + timedelta(hours=1),
) -> Session:
    return Session(
        id=session_id,
        account_id="acct-1",
        created_at=_T0,
        expires_at=expires_at,
        csrf_secret="csrf-secret",
    )


def _account_store(table: FakeTable) -> AzureTableAccountStore:
    return AzureTableAccountStore(cast(TableClient, table))


def _invite_store(table: FakeTable) -> AzureTableInviteStore:
    return AzureTableInviteStore(cast(TableClient, table))


def _session_store(table: FakeTable) -> AzureTableSessionStore:
    return AzureTableSessionStore(cast(TableClient, table))


def test_adapters_satisfy_the_repository_ports() -> None:
    table = FakeTable()

    assert isinstance(_account_store(table), AccountStore)
    assert isinstance(_invite_store(table), InviteStore)
    assert isinstance(_session_store(table), SessionStore)


def test_account_entity_uses_explicit_deterministic_serialization() -> None:
    account = _account("a@b", "Owner@Example.COM")
    entity = account_to_entity(account)

    assert entity["PartitionKey"] == "accounts"
    assert entity["RowKey"] == "a%40b"
    assert entity["Email"] == "owner@example.com"
    assert entity["CreatedAt"] == "2026-01-01T12:00:00+00:00"
    assert entity["UpdatedAt"] == "2026-01-01T12:00:00+00:00"
    assert entity["OAuthSubjects"] == '[{"provider":"github","subject":"gh-1"}]'
    assert account_from_entity(entity) == account


def test_account_store_round_trips_and_indexes_email() -> None:
    table = FakeTable()
    store = _account_store(table)
    account = _account()

    assert store.put(account) == account
    assert ("accounts", "acct-1") in table.entities
    assert ("account-emails", "owner%40example.com") in table.entities
    assert store.get("acct-1") == account
    assert store.get("missing") is None
    assert store.get_by_email("OWNER@example.com") == account
    assert store.get_by_email("nobody@example.com") is None
    assert store.list() == (account,)


def test_account_store_keeps_emails_unique_and_sorted() -> None:
    table = FakeTable()
    store = _account_store(table)
    first = _account("acct-1", "one@example.com")
    second = _account("acct-2", "two@example.com")
    store.put(second)
    store.put(first)

    assert store.list() == (first, second)
    with pytest.raises(ValueError, match="belongs to another account"):
        store.put(_account("acct-3", "one@example.com"))
    assert store.get_by_email("one@example.com") == first


def test_account_store_moves_the_email_index_on_change() -> None:
    table = FakeTable()
    store = _account_store(table)
    store.put(_account("acct-1", "old@example.com"))
    renamed = _account("acct-1", "new@example.com")

    store.put(renamed)

    assert store.get_by_email("old@example.com") is None
    assert store.get_by_email("new@example.com") == renamed
    assert ("account-emails", "old%40example.com") in table.deletes


def test_invite_entity_omits_optional_fields_and_round_trips() -> None:
    invite = _invite()
    entity = invite_to_entity(invite)

    assert entity["PartitionKey"] == "invites"
    assert entity["RowKey"] == "inv-1"
    assert entity["ExpiresAt"] == "2026-01-01T13:00:00+00:00"
    for name in ("BoundEmail", "ConsumedAt", "RevokedAt", "Note"):
        assert name not in entity
    assert invite_from_entity(entity) == invite


def test_invite_store_round_trips_by_id_and_token_hash() -> None:
    table = FakeTable()
    store = _invite_store(table)
    invite = _invite()

    assert store.put(invite) == invite
    assert ("invites", "inv-1") in table.entities
    assert ("invite-tokens", _TOKEN_HASH) in table.entities
    assert store.get("inv-1") == invite
    assert store.get("missing") is None
    assert store.get_by_token_hash(_TOKEN_HASH) == invite
    assert store.get_by_token_hash("0" * 64) is None


def test_invite_store_rejects_a_duplicate_token_hash() -> None:
    table = FakeTable()
    store = _invite_store(table)
    store.put(_invite("inv-1"))

    with pytest.raises(ValueError, match="belongs to another invite"):
        store.put(_invite("inv-2"))
    assert store.get("inv-2") is None


def test_invite_consume_marks_consumed_once() -> None:
    table = FakeTable()
    store = _invite_store(table)
    store.put(_invite())
    redeemed_at = _T0 + timedelta(minutes=5)

    receipt = store.consume("inv-1", token_hash=_TOKEN_HASH, at=redeemed_at)

    assert receipt is not None
    assert receipt.consumed_at == redeemed_at
    assert store.get("inv-1") == receipt
    assert (
        store.consume("inv-1", token_hash=_TOKEN_HASH, at=_T0 + timedelta(minutes=10))
        is None
    )


def test_invite_consume_enforces_token_hash_and_window() -> None:
    table = FakeTable()
    store = _invite_store(table)
    store.put(_invite())

    assert store.consume("inv-1", token_hash="1" * 64, at=_T0) is None
    assert store.consume("inv-1", token_hash=_TOKEN_HASH, at=_T0 - timedelta(1)) is None
    expired = _T0 + timedelta(hours=1)
    assert store.consume("inv-1", token_hash=_TOKEN_HASH, at=expired) is None
    assert store.consume("missing", token_hash=_TOKEN_HASH, at=_T0) is None
    assert store.get("inv-1") is not None


def test_invite_consume_honors_the_bound_email() -> None:
    table = FakeTable()
    store = _invite_store(table)
    store.put(_invite(bound_email="Invitee@Example.COM"))
    redeemed_at = _T0 + timedelta(minutes=1)

    assert (
        store.consume(
            "inv-1",
            token_hash=_TOKEN_HASH,
            at=redeemed_at,
            email="other@example.com",
        )
        is None
    )
    assert (
        store.consume(
            "inv-1",
            token_hash=_TOKEN_HASH,
            at=redeemed_at,
            email="invitee@example.com",
        )
        is not None
    )


def test_invite_consume_loses_the_etag_race() -> None:
    table = RacingTable()
    store = _invite_store(table)
    store.put(_invite())

    result = store.consume("inv-1", token_hash=_TOKEN_HASH, at=_T0)

    assert result is None
    assert table.updates == []
    stored_invite = store.get("inv-1")
    assert stored_invite is not None
    assert stored_invite.consumed_at is None


def test_invite_revoke_marks_once_and_respects_consumption() -> None:
    table = FakeTable()
    store = _invite_store(table)
    store.put(_invite())
    revoked_at = _T0 + timedelta(minutes=2)

    receipt = store.revoke("inv-1", at=revoked_at)

    assert receipt is not None
    assert receipt.revoked_at == revoked_at
    assert store.revoke("inv-1", at=_T0 + timedelta(minutes=3)) is None
    assert store.revoke("missing", at=_T0) is None

    consumed_table = FakeTable()
    consumed_store = _invite_store(consumed_table)
    consumed_store.put(_invite())
    consumed_store.consume("inv-1", token_hash=_TOKEN_HASH, at=_T0)
    assert consumed_store.revoke("inv-1", at=_T0 + timedelta(minutes=1)) is None


def test_session_entity_omits_revocation_and_round_trips() -> None:
    session = _session()
    entity = session_to_entity(session)

    assert entity["PartitionKey"] == "sessions"
    assert entity["RowKey"] == "sess-1"
    assert entity["CreatedAt"] == "2026-01-01T12:00:00+00:00"
    assert "RevokedAt" not in entity
    assert session_from_entity(entity) == session


def test_session_store_round_trips() -> None:
    table = FakeTable()
    store = _session_store(table)
    session = _session()

    assert store.put(session) == session
    assert ("sessions", "sess-1") in table.entities
    assert store.get("sess-1") == session
    assert store.get("missing") is None


def test_session_revoke_only_transitions_active_sessions() -> None:
    table = FakeTable()
    store = _session_store(table)
    store.put(_session())
    revoked_at = _T0 + timedelta(minutes=4)

    receipt = store.revoke("sess-1", at=revoked_at)

    assert receipt is not None
    assert receipt.revoked_at == revoked_at
    assert store.revoke("sess-1", at=_T0 + timedelta(minutes=5)) is None

    expired = FakeTable()
    expired_store = _session_store(expired)
    expired_store.put(_session("sess-expired"))
    assert store.revoke("missing", at=_T0) is None
    assert expired_store.revoke("sess-expired", at=_T0 + timedelta(hours=1)) is None


def test_session_revoke_loses_the_etag_race() -> None:
    table = RacingTable()
    store = _session_store(table)
    store.put(_session())

    assert store.revoke("sess-1", at=_T0 + timedelta(minutes=1)) is None
    stored_session = store.get("sess-1")
    assert stored_session is not None
    assert stored_session.revoked_at is None


def test_cas_transitions_require_aware_times_and_normalize_to_utc() -> None:
    table = FakeTable()
    invites = _invite_store(table)
    invites.put(_invite())
    with pytest.raises(ValueError, match="timezone-aware"):
        invites.consume("inv-1", token_hash=_TOKEN_HASH, at=datetime(2026, 1, 1, 12, 0))

    offset = timezone(timedelta(hours=2))
    receipt = invites.consume(
        "inv-1",
        token_hash=_TOKEN_HASH,
        at=datetime(2026, 1, 1, 14, 0, tzinfo=offset),
    )

    assert receipt is not None
    assert receipt.consumed_at == _T0

    sessions = _session_store(FakeTable())
    with pytest.raises(ValueError, match="timezone-aware"):
        sessions.revoke("sess-1", at=datetime(2026, 1, 1, 12, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        sessions.delete_expired(at=datetime(2026, 1, 1, 12, 0))


def test_session_delete_expired_removes_only_expired_rows() -> None:
    table = FakeTable()
    store = _session_store(table)
    active = _session("sess-active", expires_at=_T0 + timedelta(hours=3))
    short_lived = _session("sess-expired", expires_at=_T0 + timedelta(minutes=30))
    live = _session("sess-live", expires_at=_T0 + timedelta(hours=2))
    store.put(active)
    store.put(short_lived)
    store.put(live)

    removed = store.delete_expired(at=_T0 + timedelta(hours=1))

    assert removed == 1
    assert store.get("sess-expired") is None
    assert store.get("sess-active") == active
    assert store.get("sess-live") == live
    assert ("sessions", "sess-expired") in table.deletes
