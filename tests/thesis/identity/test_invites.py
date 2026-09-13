"""Deterministic offline tests for the Thesis invite lifecycle service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thesis.identity.invites import (
    InviteDeliveryError,
    InviteListingUnavailableError,
    InviteMailer,
    InviteService,
    IssuedInvite,
)
from thesis.identity.local import LocalInviteStore
from thesis.identity.models import Invite, invite_token_hash

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_WEEK = timedelta(days=7)
_BASE = "https://thesis.example.com/signup"


def _clock() -> datetime:
    return _NOW


class _RecordingMailer:
    """A mailer that records the delivery it is asked to perform."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_invite(self, *, to: str, invite_link: str, expires_at: datetime) -> None:
        self.calls.append(
            {"to": to, "invite_link": invite_link, "expires_at": expires_at}
        )


class _FailingMailer:
    """A mailer whose transport always fails."""

    def send_invite(self, *, to: str, invite_link: str, expires_at: datetime) -> None:
        raise RuntimeError("smtp is unavailable")


class _ListableLocalInviteStore(LocalInviteStore):
    """Local store extended with the listing ``list_invites`` requires."""

    def list(self) -> tuple[Invite, ...]:
        records = self._read().values()
        return tuple(sorted(records, key=lambda invite: (invite.created_at, invite.id)))


def _store(tmp_path: Path, *, listable: bool = True) -> LocalInviteStore:
    if listable:
        return _ListableLocalInviteStore(tmp_path / "invites.json")
    return LocalInviteStore(tmp_path / "invites.json")


def test_create_returns_link_once_and_persists_only_hash(tmp_path: Path) -> None:
    service = InviteService(_store(tmp_path), clock=_clock)

    issued = service.create_invite(
        email="Invitee@Example.com",
        role="member",
        expires_in=_WEEK,
        note="welcome",
        created_by="acct-1",
        invite_link_base=_BASE,
    )

    assert isinstance(issued, IssuedInvite)
    assert issued.invite_link == f"{_BASE}?token={issued.token}"
    assert issued.invite.token_hash == invite_token_hash(issued.token)
    assert issued.invite.bound_email == "invitee@example.com"
    assert issued.invite.expires_at == _NOW + _WEEK
    assert issued.invite.created_at == _NOW
    assert issued.invite.note == "welcome"

    document = (tmp_path / "invites.json").read_text(encoding="utf-8")
    assert issued.token not in document
    assert issued.invite.token_hash in document
    assert not hasattr(issued.invite, "token")

    # The raw credential is excluded from any diagnostic rendering.
    assert issued.token not in repr(issued)
    assert issued.invite_link not in repr(issued)

    second = service.create_invite(
        role="member",
        expires_in=_WEEK,
        created_by="acct-1",
        invite_link_base=_BASE,
    )
    assert second.token != issued.token
    assert second.invite.id != issued.invite.id
    assert second.invite.bound_email is None


def test_list_invites_never_returns_raw_tokens(tmp_path: Path) -> None:
    service = InviteService(_store(tmp_path), clock=_clock)
    issued = service.create_invite(
        email="invitee@example.com",
        expires_in=_WEEK,
        created_by="acct-1",
        invite_link_base=_BASE,
    )

    invites = service.list_invites()

    assert [invite.id for invite in invites] == [issued.invite.id]
    assert invites[0].token_hash == invite_token_hash(issued.token)
    assert not any(hasattr(invite, "token") for invite in invites)
    assert issued.token not in repr(invites)


def test_list_invites_requires_a_directory_store(tmp_path: Path) -> None:
    service = InviteService(_store(tmp_path, listable=False), clock=_clock)

    with pytest.raises(InviteListingUnavailableError):
        service.list_invites()


def test_revoke_works_before_redemption_and_fails_after(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = InviteService(store, clock=_clock)
    issued = service.create_invite(
        email="invitee@example.com",
        expires_in=_WEEK,
        created_by="acct-1",
        invite_link_base=_BASE,
    )
    revoked_at = _NOW + timedelta(hours=1)

    assert service.revoke_invite(issued.invite.id, at=revoked_at) is True
    assert service.revoke_invite(issued.invite.id, at=revoked_at) is False
    assert service.list_invites()[0].revoked_at == revoked_at
    assert (
        store.consume(
            issued.invite.id,
            token_hash=issued.invite.token_hash,
            at=_NOW + timedelta(days=1),
            email="invitee@example.com",
        )
        is None
    )

    redeemed = service.create_invite(
        email="other@example.com",
        expires_in=_WEEK,
        created_by="acct-1",
        invite_link_base=_BASE,
    )
    receipt = store.consume(
        redeemed.invite.id,
        token_hash=redeemed.invite.token_hash,
        at=_NOW + timedelta(hours=2),
        email="other@example.com",
    )
    assert receipt is not None
    assert service.revoke_invite(redeemed.invite.id, at=_NOW + timedelta(hours=3)) is (
        False
    )


def test_expiry_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = InviteService(store, clock=_clock)
    issued = service.create_invite(
        email="invitee@example.com",
        expires_in=timedelta(hours=1),
        created_by="acct-1",
        invite_link_base=_BASE,
    )
    expired_at = _NOW + timedelta(hours=1)

    assert issued.invite.expires_at == expired_at
    assert issued.invite.is_redeemable(_NOW) is True
    assert issued.invite.is_redeemable(expired_at) is False
    assert (
        store.consume(
            issued.invite.id,
            token_hash=issued.invite.token_hash,
            at=expired_at,
            email="invitee@example.com",
        )
        is None
    )


def test_mailer_receives_link_and_expiry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mailer = _RecordingMailer()
    service = InviteService(store, clock=_clock, mailer=mailer)

    issued = service.create_invite(
        email="Invitee@Example.com",
        expires_in=_WEEK,
        created_by="acct-1",
        invite_link_base=_BASE,
    )

    assert mailer.calls == [
        {
            "to": "invitee@example.com",
            "invite_link": issued.invite_link,
            "expires_at": issued.invite.expires_at,
        }
    ]
    assert store.get(issued.invite.id) == issued.invite


def test_mailer_protocol_is_structural() -> None:
    assert isinstance(_RecordingMailer(), InviteMailer)
    assert isinstance(_FailingMailer(), InviteMailer)


def test_failing_mailer_raises_without_leaking_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = InviteService(store, clock=_clock, mailer=_FailingMailer())

    with pytest.raises(InviteDeliveryError) as caught:
        service.create_invite(
            email="invitee@example.com",
            expires_in=_WEEK,
            created_by="acct-1",
            invite_link_base=_BASE,
        )

    message = str(caught.value)
    assert "invitation" in message
    assert "token" not in message.lower()
    assert _BASE not in message
    assert isinstance(caught.value.__cause__, RuntimeError)
    # Delivery happens before the write, so a failed send leaves no orphan.
    assert store.list() == ()


def test_create_rejects_invalid_inputs(tmp_path: Path) -> None:
    service = InviteService(_store(tmp_path), clock=_clock)

    with pytest.raises(ValueError, match="expires_in"):
        service.create_invite(
            expires_in=timedelta(0), created_by="acct-1", invite_link_base=_BASE
        )
    with pytest.raises(ValueError, match="invite_link_base"):
        service.create_invite(
            expires_in=_WEEK, created_by="acct-1", invite_link_base="   "
        )
    with pytest.raises(ValueError, match="email"):
        service.create_invite(
            email="not-an-email",
            expires_in=_WEEK,
            created_by="acct-1",
            invite_link_base=_BASE,
        )


def test_revoke_requires_an_aware_timestamp(tmp_path: Path) -> None:
    service = InviteService(_store(tmp_path), clock=_clock)

    with pytest.raises(ValueError, match="timezone-aware"):
        service.revoke_invite("inv-missing", at=datetime(2026, 1, 1))
