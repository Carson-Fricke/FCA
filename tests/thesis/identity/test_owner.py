"""Deterministic offline tests for default-owner seeding and claiming."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thesis.identity.local import (
    LocalAccountStore,
    LocalIdentityStore,
    seed_local_owner,
)
from thesis.identity.models import Account, AccountRole, AccountStatus
from thesis.identity.owner import (
    DEFAULT_WORKSPACE_OWNER_ID,
    OWNER_EMAIL_ENV_VAR,
    OwnerClaim,
    OwnerConfigurationError,
    claim_owner,
    derive_owner_account_id,
    owner_account_map,
    seed_owner_account,
    seed_owner_from_environment,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_EARLIER = _NOW - timedelta(days=1)
_OWNER_EMAIL = "Owner@Example.com"


def _clock() -> datetime:
    return _NOW


def _store(tmp_path: Path) -> LocalAccountStore:
    return LocalAccountStore(tmp_path / "accounts.json")


def _existing_account(
    *,
    account_id: str = "acct-existing",
    email: str = _OWNER_EMAIL,
    role: AccountRole = "member",
    status: AccountStatus = "active",
) -> Account:
    return Account(
        id=account_id,
        email=email,
        display_name="Existing",
        role=role,
        status=status,
        created_at=_EARLIER,
        updated_at=_EARLIER,
    )


def test_seed_owner_account_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = seed_owner_account(store, email=_OWNER_EMAIL, clock=_clock)
    second = seed_owner_account(store, email=_OWNER_EMAIL, clock=_clock)

    assert first.id == second.id
    assert first.role == "owner"
    assert first.status == "active"
    assert first.email == "owner@example.com"
    assert first.created_at == _NOW
    assert second.updated_at == _NOW
    assert len(store.list()) == 1


def test_seed_owner_account_reuses_and_promotes_existing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(_existing_account(role="member", status="disabled"))

    seeded = seed_owner_account(store, email=_OWNER_EMAIL, clock=_clock)

    assert seeded.id == "acct-existing"
    assert seeded.role == "owner"
    assert seeded.status == "active"
    assert seeded.created_at == _EARLIER
    assert seeded.updated_at == _NOW
    assert len(store.list()) == 1


def test_seed_owner_account_returns_active_owner_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(_existing_account(role="owner", status="active"))

    seeded = seed_owner_account(store, email=_OWNER_EMAIL, clock=_clock)

    assert seeded.updated_at == _EARLIER
    assert len(store.list()) == 1


def test_seed_owner_account_rejects_invalid_email(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(OwnerConfigurationError):
        seed_owner_account(store, email="not-an-email", clock=_clock)

    assert store.list() == ()


def test_seed_owner_from_environment_unset_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)

    seeded = seed_owner_from_environment(store, environ={}, clock=_clock)

    assert seeded is None
    assert store.list() == ()


def test_seed_owner_from_environment_blank_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)

    seeded = seed_owner_from_environment(
        store, environ={OWNER_EMAIL_ENV_VAR: "   "}, clock=_clock
    )

    assert seeded is None
    assert store.list() == ()


def test_seed_owner_from_environment_seeds_when_set(tmp_path: Path) -> None:
    store = _store(tmp_path)

    seeded = seed_owner_from_environment(
        store, environ={OWNER_EMAIL_ENV_VAR: _OWNER_EMAIL}, clock=_clock
    )

    assert seeded is not None
    assert seeded.role == "owner"
    assert seeded.email == "owner@example.com"
    assert len(store.list()) == 1


def test_derive_owner_account_id_is_deterministic_and_normalized() -> None:
    first = derive_owner_account_id(_OWNER_EMAIL)
    second = derive_owner_account_id("  owner@example.com  ")

    assert first == second
    assert first.startswith("owner-")


def test_owner_account_map_maps_owner_to_default_workspace_owner() -> None:
    mapping = owner_account_map("owner-abc")

    assert mapping("owner-abc") == DEFAULT_WORKSPACE_OWNER_ID
    assert mapping("someone-else") == "someone-else"


def test_owner_account_map_is_idempotent_across_repeated_calls() -> None:
    first = owner_account_map("owner-abc")
    second = owner_account_map("owner-abc")

    assert first("owner-abc") == second("owner-abc") == DEFAULT_WORKSPACE_OWNER_ID
    assert first("other") == second("other") == "other"


def test_owner_account_map_rejects_empty_ids() -> None:
    with pytest.raises(OwnerConfigurationError):
        owner_account_map("")
    with pytest.raises(OwnerConfigurationError):
        owner_account_map("owner-abc", workspace_owner_id="")


def test_owner_claim_uses_seeded_account_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    account = seed_owner_account(store, email=_OWNER_EMAIL, clock=_clock)

    claim = OwnerClaim(owner_account_id=account.id)
    mapping = claim.account_owner_map()

    assert mapping(account.id) == DEFAULT_WORKSPACE_OWNER_ID
    assert mapping("other") == "other"


def test_claim_owner_seeds_and_claims_idempotently(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = claim_owner(store, email=_OWNER_EMAIL, clock=_clock)
    second = claim_owner(store, email=_OWNER_EMAIL, clock=_clock)

    assert first == second
    assert first.account_owner_map()(first.owner_account_id) == (
        DEFAULT_WORKSPACE_OWNER_ID
    )
    assert len(store.list()) == 1


def test_seed_local_owner_seeds_into_local_identity_store(tmp_path: Path) -> None:
    identity = LocalIdentityStore.open(tmp_path)

    seeded = seed_local_owner(identity, email=_OWNER_EMAIL, clock=_clock)

    assert seeded.role == "owner"
    assert identity.accounts.get_by_email("owner@example.com") == seeded
