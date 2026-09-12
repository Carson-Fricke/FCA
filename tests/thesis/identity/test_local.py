"""Deterministic offline tests for the local JSON identity stores."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thesis.identity.local import (
    LocalAccountStore,
    LocalIdentityStore,
    LocalIdentityStoreError,
    LocalInviteStore,
    LocalSessionStore,
    default_local_identity_root,
)
from thesis.identity.models import (
    Account,
    Invite,
    OAuthSubject,
    Session,
    invite_token_hash,
)
from thesis.identity.ports import AccountStore, InviteStore, SessionStore

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_EXPIRES = _CREATED + timedelta(days=7)


def _account(
    account_id: str = "acct-1",
    email: str = "Owner@Example.com",
    *,
    subjects: tuple[OAuthSubject, ...] = (),
    display_name: str = "Owner",
    updated_at: datetime = _CREATED,
) -> Account:
    return Account(
        id=account_id,
        email=email,
        display_name=display_name,
        oauth_subjects=subjects,
        role="owner",
        status="active",
        created_at=_CREATED,
        updated_at=updated_at,
    )


def _invite(
    invite_id: str = "inv-1",
    *,
    raw_token: str = "raw-token",
    bound_email: str | None = None,
    expires_at: datetime = _EXPIRES,
) -> Invite:
    return Invite(
        id=invite_id,
        token_hash=invite_token_hash(raw_token),
        bound_email=bound_email,
        role="member",
        expires_at=expires_at,
        created_by="acct-1",
        created_at=_CREATED,
    )


def _session(
    session_id: str = "sess-1",
    *,
    expires_at: datetime = _EXPIRES,
) -> Session:
    return Session(
        id=session_id,
        account_id="acct-1",
        created_at=_CREATED,
        expires_at=expires_at,
        csrf_secret="csrf-secret-1",
    )


def test_stores_satisfy_the_ports(tmp_path: Path) -> None:
    assert isinstance(LocalAccountStore(tmp_path / "accounts.json"), AccountStore)
    assert isinstance(LocalInviteStore(tmp_path / "invites.json"), InviteStore)
    assert isinstance(LocalSessionStore(tmp_path / "sessions.json"), SessionStore)


def test_account_round_trip_persists_and_orders_by_id(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    store = LocalAccountStore(path)
    later = _account("acct-b", "B@Example.com")
    earlier = _account("acct-a", "A@Example.com")

    store.put(later)
    store.put(earlier)

    assert store.get("acct-a") == earlier
    assert store.get("missing") is None
    assert [account.id for account in store.list()] == ["acct-a", "acct-b"]

    reopened = LocalAccountStore(path)
    assert reopened.list() == (earlier, later)
    assert reopened.get_by_email("b@example.com") == later
    assert reopened.get_by_email("B@EXAMPLE.COM") == later
    assert reopened.get_by_email("missing@example.com") is None
    assert '"email": "a@example.com"' in path.read_text(encoding="utf-8")


def test_account_email_index_is_unique(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path / "accounts.json")
    store.put(_account("acct-1", "Owner@Example.com"))

    with pytest.raises(ValueError, match="already bound"):
        store.put(_account("acct-2", "owner@example.com"))


def test_account_replacement_by_id_is_allowed(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path / "accounts.json")
    store.put(_account())

    renamed = _account(display_name="Renamed", updated_at=_CREATED + timedelta(hours=1))
    assert store.put(renamed) == renamed
    assert store.get("acct-1") == renamed


def test_oauth_subjects_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    account = _account(
        subjects=(
            OAuthSubject(provider="google", subject="google-subject"),
            OAuthSubject(provider="entra", subject="entra-subject"),
        )
    )
    LocalAccountStore(path).put(account)

    assert LocalAccountStore(path).get("acct-1") == account


def test_invite_round_trip_by_id_and_token_hash(tmp_path: Path) -> None:
    path = tmp_path / "invites.json"
    invite = _invite()
    store = LocalInviteStore(path)
    store.put(invite)

    assert store.get("inv-1") == invite
    assert store.get("missing") is None
    assert store.get_by_token_hash(invite.token_hash) == invite
    assert store.get_by_token_hash(invite_token_hash("other")) is None

    reopened = LocalInviteStore(path)
    assert reopened.get("inv-1") == invite
    assert reopened.get_by_token_hash(invite.token_hash) == invite


def test_invite_consume_is_single_use(tmp_path: Path) -> None:
    path = tmp_path / "invites.json"
    store = LocalInviteStore(path)
    store.put(_invite())
    redeemed_at = _CREATED + timedelta(days=1)

    receipt = store.consume(
        "inv-1", token_hash=invite_token_hash("raw-token"), at=redeemed_at
    )

    assert receipt is not None
    assert receipt.consumed_at == redeemed_at
    assert not receipt.is_redeemable(redeemed_at)
    assert (
        store.consume(
            "inv-1", token_hash=invite_token_hash("raw-token"), at=redeemed_at
        )
        is None
    )
    assert LocalInviteStore(path).get("inv-1") == receipt


def test_invite_consume_rejects_wrong_token_and_expiry(tmp_path: Path) -> None:
    store = LocalInviteStore(tmp_path / "invites.json")
    store.put(_invite())

    assert (
        store.consume("inv-1", token_hash=invite_token_hash("wrong"), at=_CREATED)
        is None
    )
    assert (
        store.consume("inv-1", token_hash=invite_token_hash("raw-token"), at=_EXPIRES)
        is None
    )
    assert (
        store.consume("missing", token_hash=invite_token_hash("raw-token"), at=_CREATED)
        is None
    )
    assert store.get("inv-1") is not None


def test_invite_consume_rejects_revoked_invite(tmp_path: Path) -> None:
    store = LocalInviteStore(tmp_path / "invites.json")
    store.put(_invite())
    assert store.revoke("inv-1", at=_CREATED + timedelta(hours=1)) is not None

    assert (
        store.consume(
            "inv-1",
            token_hash=invite_token_hash("raw-token"),
            at=_CREATED + timedelta(days=1),
        )
        is None
    )


def test_invite_consume_enforces_email_binding(tmp_path: Path) -> None:
    store = LocalInviteStore(tmp_path / "invites.json")
    store.put(_invite(bound_email="Invitee@Example.com"))
    redeemed_at = _CREATED + timedelta(days=1)

    assert (
        store.consume(
            "inv-1",
            token_hash=invite_token_hash("raw-token"),
            at=redeemed_at,
            email="intruder@example.com",
        )
        is None
    )
    receipt = store.consume(
        "inv-1",
        token_hash=invite_token_hash("raw-token"),
        at=redeemed_at,
        email="INVITEE@example.com",
    )
    assert receipt is not None
    assert receipt.consumed_at == redeemed_at


def test_invite_revoke_is_single_and_respects_consumption(tmp_path: Path) -> None:
    store = LocalInviteStore(tmp_path / "invites.json")
    store.put(_invite())
    revoked_at = _CREATED + timedelta(hours=1)

    revoked = store.revoke("inv-1", at=revoked_at)
    assert revoked is not None
    assert revoked.revoked_at == revoked_at
    assert store.revoke("inv-1", at=revoked_at) is None
    assert store.revoke("missing", at=revoked_at) is None

    consumed_store = LocalInviteStore(tmp_path / "consumed.json")
    consumed_store.put(_invite("inv-2"))
    consumed = consumed_store.consume(
        "inv-2", token_hash=invite_token_hash("raw-token"), at=_CREATED
    )
    assert consumed is not None
    assert consumed_store.revoke("inv-2", at=revoked_at) is None


def test_invite_consume_race_has_exactly_one_winner(tmp_path: Path) -> None:
    store = LocalInviteStore(tmp_path / "invites.json")
    invite = _invite()
    store.put(invite)
    redeemed_at = _CREATED + timedelta(days=1)
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[Invite | None] = []
    results_lock = threading.Lock()

    def redeem() -> None:
        barrier.wait()
        outcome = store.consume(invite.id, token_hash=invite.token_hash, at=redeemed_at)
        with results_lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(redeem) for _ in range(workers)]
        for future in futures:
            future.result()

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].consumed_at == redeemed_at
    assert (
        store.consume(invite.id, token_hash=invite.token_hash, at=redeemed_at) is None
    )


def test_session_round_trip_and_revoke(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = LocalSessionStore(path)
    session = _session()
    store.put(session)
    revoked_at = _CREATED + timedelta(hours=1)

    assert store.get("sess-1") == session
    assert store.get("missing") is None
    revoked = store.revoke("sess-1", at=revoked_at)
    assert revoked is not None
    assert revoked.revoked_at == revoked_at
    assert store.revoke("sess-1", at=revoked_at) is None
    assert store.get("sess-1") == revoked
    assert LocalSessionStore(path).get("sess-1") == revoked


def test_session_delete_expired_removes_only_expired(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = LocalSessionStore(path)
    active = _session("sess-active")
    stale = _session("sess-stale", expires_at=_CREATED + timedelta(hours=1))
    store.put(active)
    store.put(stale)
    checked_at = _CREATED + timedelta(days=1)

    assert store.delete_expired(at=checked_at) == 1
    assert store.get("sess-stale") is None
    assert store.get("sess-active") == active

    reopened = LocalSessionStore(path)
    assert reopened.get("sess-stale") is None
    assert reopened.get("sess-active") == active
    assert reopened.delete_expired(at=checked_at) == 0


def test_session_revoke_rejects_expired_session(tmp_path: Path) -> None:
    store = LocalSessionStore(tmp_path / "sessions.json")
    store.put(_session(expires_at=_CREATED + timedelta(hours=1)))

    assert store.revoke("sess-1", at=_CREATED + timedelta(days=1)) is None


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        '{"schema_version": 1}',
        '{"schema_version": 2, "invites": []}',
        '{"schema_version": 1, "invites": {}}',
    ],
)
def test_malformed_invite_store_is_rejected(tmp_path: Path, document: str) -> None:
    path = tmp_path / "invites.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(LocalIdentityStoreError, match="is invalid"):
        LocalInviteStore(path).get("inv-1")

    assert path.read_text(encoding="utf-8") == document


def test_duplicate_invite_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invites.json"
    row = _invite().model_dump(mode="json")
    payload = {"schema_version": 1, "invites": [row, row]}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LocalIdentityStoreError, match="is invalid"):
        LocalInviteStore(path).get("inv-1")


def test_default_root_follows_local_convention() -> None:
    root = default_local_identity_root()

    assert root.parts[-3:] == (".local", "thesis", "identity")
    assert root.is_absolute()
    store = LocalIdentityStore.open()
    assert store.accounts.path == root / "accounts.json"
    assert store.invites.path == root / "invites.json"
    assert store.sessions.path == root / "sessions.json"


def test_facade_opens_three_adapters_over_one_root(tmp_path: Path) -> None:
    root = (tmp_path / "identity").resolve()
    stores = LocalIdentityStore.open(tmp_path / "identity")

    assert stores.accounts.path.parent == root
    assert stores.invites.path.parent == root
    assert stores.sessions.path.parent == root
    assert stores.accounts.path.name == "accounts.json"
    assert stores.invites.path.name == "invites.json"
    assert stores.sessions.path.name == "sessions.json"

    stores.accounts.put(_account())
    assert stores.accounts.get("acct-1") == _account()
