"""Deterministic offline tests for one-time invite redemption."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thesis.identity.local import LocalAccountStore, LocalInviteStore
from thesis.identity.models import (
    AccountRole,
    Invite,
    OAuthSubject,
    invite_token_hash,
)
from thesis.identity.redemption import (
    InvalidInviteError,
    InviteEmailMismatchError,
    RedemptionAuditEvent,
    RedemptionAuditSink,
    RedemptionError,
    RedemptionRateLimited,
    RedemptionRateLimiter,
    RedemptionResult,
    RedemptionService,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_WEEK = timedelta(days=7)


def _clock() -> datetime:
    return _NOW


def _invite(
    *,
    invite_id: str = "inv-1",
    token: str = "invite-token",
    email: str | None = "invitee@example.com",
    role: AccountRole = "member",
    created_at: datetime = _NOW,
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Invite:
    return Invite(
        id=invite_id,
        token_hash=invite_token_hash(token),
        bound_email=email,
        role=role,
        expires_at=expires_at if expires_at is not None else created_at + _WEEK,
        consumed_at=consumed_at,
        revoked_at=revoked_at,
        created_by="acct-admin",
        created_at=created_at,
    )


class _CountingLimiter:
    """A limiter that allows the first ``limit`` attempts and records keys."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls: list[tuple[str, datetime]] = []

    def allow(self, key: str, *, at: datetime) -> bool:
        self.calls.append((key, at))
        return len(self.calls) <= self.limit


class _RecordingAudit:
    """An audit sink that records every event it receives."""

    def __init__(self) -> None:
        self.events: list[RedemptionAuditEvent] = []

    def record(self, event: RedemptionAuditEvent) -> None:
        self.events.append(event)


def _stores(tmp_path: Path) -> tuple[LocalInviteStore, LocalAccountStore]:
    return (
        LocalInviteStore(tmp_path / "invites.json"),
        LocalAccountStore(tmp_path / "accounts.json"),
    )


def _stored_invite(invites: LocalInviteStore, invite_id: str) -> Invite:
    stored = invites.get(invite_id)
    assert stored is not None
    return stored


def test_happy_path_consumes_invite_and_creates_bound_account(
    tmp_path: Path,
) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc", email="Invitee@Example.com", role="admin"))
    audit = _RecordingAudit()
    service = RedemptionService(invites, accounts, _clock, audit=audit)
    subject = OAuthSubject(provider="entra", subject="sub-1")

    result = service.redeem(
        token="tok-abc",
        email="invitee@example.com",
        display_name="Invitee",
        oauth_subjects=(subject,),
    )

    assert isinstance(result, RedemptionResult)
    assert result.invite.consumed_at == _NOW
    assert result.account.email == "invitee@example.com"
    assert result.account.role == "admin"
    assert result.account.status == "active"
    assert result.account.display_name == "Invitee"
    assert result.account.oauth_subjects == (subject,)
    assert result.account.created_at == _NOW
    assert result.account.updated_at == _NOW
    assert accounts.get_by_email("invitee@example.com") == result.account
    assert _stored_invite(invites, "inv-1").consumed_at == _NOW
    assert audit.events == [
        RedemptionAuditEvent(
            outcome="redeemed",
            at=_NOW,
            invite_id="inv-1",
            email="invitee@example.com",
            reason=None,
            client_key=None,
        )
    ]


def test_display_name_defaults_to_email_local_part(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc"))
    service = RedemptionService(invites, accounts, _clock)

    result = service.redeem(token="tok-abc", email="invitee@example.com")

    assert result.account.display_name == "invitee"


def test_unbound_invite_accepts_any_email(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc", email=None))
    service = RedemptionService(invites, accounts, _clock)

    result = service.redeem(token="tok-abc", email="anyone@example.com")

    assert result.account.email == "anyone@example.com"


def test_second_redeem_with_same_token_fails(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc"))
    service = RedemptionService(invites, accounts, _clock)

    service.redeem(token="tok-abc", email="invitee@example.com")

    with pytest.raises(InvalidInviteError, match="already been redeemed"):
        service.redeem(token="tok-abc", email="invitee@example.com")
    assert len(accounts.list()) == 1


def test_concurrent_redeem_yields_exactly_one_success(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="race-token"))
    service = RedemptionService(invites, accounts, _clock)
    barrier = threading.Barrier(2)
    successes: list[RedemptionResult] = []
    failures: list[RedemptionError] = []

    def attempt() -> None:
        barrier.wait()
        try:
            successes.append(
                service.redeem(token="race-token", email="invitee@example.com")
            )
        except RedemptionError as error:
            failures.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InvalidInviteError)
    assert len(accounts.list()) == 1
    assert _stored_invite(invites, "inv-1").consumed_at == _NOW


def test_unknown_token_rejects(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc"))
    service = RedemptionService(invites, accounts, _clock)

    with pytest.raises(InvalidInviteError, match="not recognized"):
        service.redeem(token="wrong-token", email="invitee@example.com")


def test_expired_invite_rejects(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(
        _invite(
            token="tok-abc",
            created_at=_NOW - timedelta(days=2),
            expires_at=_NOW - timedelta(days=1),
        )
    )
    service = RedemptionService(invites, accounts, _clock)

    with pytest.raises(InvalidInviteError, match="expired"):
        service.redeem(token="tok-abc", email="invitee@example.com")


def test_revoked_invite_rejects(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(
        _invite(
            token="tok-abc",
            created_at=_NOW - timedelta(days=2),
            revoked_at=_NOW - timedelta(hours=1),
        )
    )
    service = RedemptionService(invites, accounts, _clock)

    with pytest.raises(InvalidInviteError, match="revoked"):
        service.redeem(token="tok-abc", email="invitee@example.com")


def test_email_binding_mismatch_rejects(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc", email="bound@example.com"))
    service = RedemptionService(invites, accounts, _clock)

    with pytest.raises(InviteEmailMismatchError):
        service.redeem(token="tok-abc", email="other@example.com")
    assert _stored_invite(invites, "inv-1").consumed_at is None


def test_existing_account_rejects_without_consuming_invite(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc"))
    service = RedemptionService(invites, accounts, _clock)
    service.redeem(token="tok-abc", email="invitee@example.com")

    invites.put(_invite(invite_id="inv-2", token="tok-second"))
    with pytest.raises(InvalidInviteError, match="already exists"):
        service.redeem(token="tok-second", email="invitee@example.com")
    assert _stored_invite(invites, "inv-2").consumed_at is None


def test_rate_limit_triggers_and_emits_failure_audit(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="valid-token"))
    limiter = _CountingLimiter(limit=1)
    audit = _RecordingAudit()
    service = RedemptionService(
        invites, accounts, _clock, audit=audit, rate_limiter=limiter
    )

    with pytest.raises(InvalidInviteError):
        service.redeem(
            token="unknown-token", email="invitee@example.com", client_key="client-1"
        )
    with pytest.raises(RedemptionRateLimited):
        service.redeem(
            token="valid-token", email="invitee@example.com", client_key="client-1"
        )

    assert audit.events[-1].outcome == "rejected"
    assert audit.events[-1].reason == "rate_limited"
    assert _stored_invite(invites, "inv-1").consumed_at is None
    assert all(
        "valid-token" not in key and "unknown-token" not in key
        for key, _ in limiter.calls
    )


def test_rate_limit_key_falls_back_to_token_hash(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="valid-token"))
    limiter = _CountingLimiter(limit=1)
    service = RedemptionService(invites, accounts, _clock, rate_limiter=limiter)

    service.redeem(token="valid-token", email="invitee@example.com")

    assert limiter.calls[0][0] == f"token:{invite_token_hash('valid-token')}"


def test_audit_events_never_contain_token_material(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    secret_token = "super-secret-invite-token-value"
    invites.put(_invite(token=secret_token))
    audit = _RecordingAudit()
    service = RedemptionService(invites, accounts, _clock, audit=audit)

    service.redeem(token=secret_token, email="invitee@example.com")
    with pytest.raises(InvalidInviteError):
        service.redeem(token="another-secret-token", email="invitee@example.com")

    rendered = repr(audit.events)
    assert secret_token not in rendered
    assert "another-secret-token" not in rendered
    assert invite_token_hash(secret_token) not in rendered
    assert [event.outcome for event in audit.events] == ["redeemed", "rejected"]
    assert not any(hasattr(event, "token") for event in audit.events)


def test_audit_and_limiter_protocols_are_structural() -> None:
    assert isinstance(_RecordingAudit(), RedemptionAuditSink)
    assert isinstance(_CountingLimiter(1), RedemptionRateLimiter)


def test_redeem_requires_an_aware_clock(tmp_path: Path) -> None:
    invites, accounts = _stores(tmp_path)
    invites.put(_invite(token="tok-abc"))
    service = RedemptionService(invites, accounts, lambda: datetime(2026, 1, 1))

    with pytest.raises(ValueError, match="timezone-aware"):
        service.redeem(token="tok-abc", email="invitee@example.com")
