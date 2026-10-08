"""Offline hardening drills for public auth: audit, revocation, rate limits.

The suite is deterministic and needs no network, no wall clock, and no
application wiring.  It composes the identity audit trail and rate-limit
primitives directly, exercises a real T311 ``RedemptionService`` through the
unified sink adapter, and runs a revocation drill against the session
authenticator built on an in-memory session store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from thesis.agent.residency import OperationAuditRecord
from thesis.auth import (
    BrowserSecurity,
    OperatorSessionService,
    RuntimeSecurity,
    WorkspaceAuthorizer,
)
from thesis.http.auth import SESSION_COOKIE_NAME, SessionCookieAuthenticator
from thesis.identity.audit import (
    FixedWindowRateLimiter,
    IdentityAuditEvent,
    IdentityAuditTrail,
    IdentityRateLimiters,
    RateLimitPolicy,
    RecordingIdentityAuditSink,
    RedemptionIdentityAuditSink,
)
from thesis.identity.local import LocalAccountStore, LocalInviteStore
from thesis.identity.models import Invite, Session, invite_token_hash
from thesis.identity.redemption import (
    RedemptionRateLimited,
    RedemptionService,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE = "workspace-a"
ACCOUNT_ID = "acct-1"
_MINUTE = timedelta(minutes=1)


def _clock() -> datetime:
    return NOW


def _invite(
    *,
    invite_id: str = "inv-1",
    token: str = "invite-token",
    email: str | None = "invitee@example.com",
) -> Invite:
    return Invite(
        id=invite_id,
        token_hash=invite_token_hash(token),
        bound_email=email,
        role="member",
        expires_at=NOW + timedelta(days=7),
        created_by="acct-admin",
        created_at=NOW,
    )


# ---------------------------------------------------------------------------
# Revocation drill: server-side revocation invalidates the very next request.
# ---------------------------------------------------------------------------


class _MemorySessionStore:
    """In-memory :class:`~thesis.identity.ports.SessionStore` double."""

    def __init__(self) -> None:
        self._records: dict[str, Session] = {}

    def get(self, session_id: str) -> Session | None:
        return self._records.get(session_id)

    def put(self, session: Session) -> Session:
        self._records[session.id] = session
        return session

    def revoke(self, session_id: str, *, at: datetime) -> Session | None:
        record = self._records.get(session_id)
        if record is None or not record.is_active(at):
            return None
        revoked = record.model_copy(update={"revoked_at": at})
        self._records[session_id] = revoked
        return revoked

    def delete_expired(self, *, at: datetime) -> int:
        expired = [
            session_id
            for session_id, record in self._records.items()
            if at >= record.expires_at
        ]
        for session_id in expired:
            del self._records[session_id]
        return len(expired)


class _MutableClock:
    """A deterministic clock a test can advance without wall time."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _session_service(
    store: _MemorySessionStore, clock: _MutableClock
) -> OperatorSessionService:
    tokens = iter(("tok-0001", "csrf-0001", "tok-0002", "csrf-0002"))
    return OperatorSessionService(
        WorkspaceAuthorizer(
            lambda workspace_id: ACCOUNT_ID if workspace_id == WORKSPACE else None
        ),
        RuntimeSecurity(browser=BrowserSecurity(secure_cookies=True, same_site="lax")),
        session_store=store,
        clock=clock,
        token_factory=lambda: next(tokens),
    )


def _guard_app(store: _MemorySessionStore, clock: _MutableClock) -> FastAPI:
    """A minimal app whose only route is protected by the cookie authenticator."""
    authenticator = SessionCookieAuthenticator(session_store=store, clock=clock)
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        principal = await authenticator(request, None)
        return {"principal": principal}

    return app


def test_revocation_drill_rejects_the_very_next_request() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    service = _session_service(store, clock)
    session = service.open_session(
        ACCOUNT_ID, WORKSPACE, authenticated_operator_id=ACCOUNT_ID
    )
    client = TestClient(_guard_app(store, clock), base_url="https://testserver")
    client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

    authenticated = client.get("/whoami")
    assert authenticated.status_code == 200
    assert authenticated.json()["principal"] == f"session:{ACCOUNT_ID}"

    service.revoke_session(session.session_id)

    # The very next request must fail closed: revocation is effective within
    # one request, not after a cache TTL or a reconnect.
    rejected = client.get("/whoami")
    assert rejected.status_code == 401


def test_revoked_session_cannot_be_reauthorized_by_replay() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    service = _session_service(store, clock)
    session = service.open_session(
        ACCOUNT_ID, WORKSPACE, authenticated_operator_id=ACCOUNT_ID
    )
    service.revoke_session(session.session_id)

    client = TestClient(_guard_app(store, clock), base_url="https://testserver")
    client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

    assert client.get("/whoami").status_code == 401


# ---------------------------------------------------------------------------
# Rate limits: login, invite, and redemption primitives fail closed.
# ---------------------------------------------------------------------------


def test_fixed_window_limiter_rejects_after_budget() -> None:
    limiter = FixedWindowRateLimiter(RateLimitPolicy(attempts=3, window=_MINUTE))

    assert [limiter.allow("login:client-1", at=NOW) for _ in range(3)] == [
        True,
        True,
        True,
    ]
    assert limiter.allow("login:client-1", at=NOW) is False
    # A different key keeps its own budget.
    assert limiter.allow("login:client-2", at=NOW) is True
    # The window rolls on the first attempt at or after the boundary.
    assert limiter.allow("login:client-1", at=NOW + _MINUTE) is True


def test_identity_rate_limiters_cover_login_invite_and_redemption() -> None:
    limiters = IdentityRateLimiters.from_policies(
        login=RateLimitPolicy(2, _MINUTE),
        invite=RateLimitPolicy(1, _MINUTE),
        redemption=RateLimitPolicy(2, _MINUTE),
    )

    login = limiters.limiter_for("login")
    invite = limiters.limiter_for("invite")
    redemption = limiters.limiter_for("redemption")

    assert [login.allow("client-1", at=NOW) for _ in range(3)] == [True, True, False]
    assert [invite.allow("acct-1", at=NOW) for _ in range(2)] == [True, False]
    assert [redemption.allow("client-1", at=NOW) for _ in range(3)] == [
        True,
        True,
        False,
    ]


def test_redemption_service_enforces_its_injected_limiter(tmp_path: Path) -> None:
    invites = LocalInviteStore(tmp_path / "invites.json")
    accounts = LocalAccountStore(tmp_path / "accounts.json")
    invites.put(_invite(token="tok-1"))
    limiter = FixedWindowRateLimiter(RateLimitPolicy(attempts=1, window=_MINUTE))
    service = RedemptionService(invites, accounts, _clock, rate_limiter=limiter)

    service.redeem(token="tok-1", email="invitee@example.com", client_key="client-1")

    with pytest.raises(RedemptionRateLimited):
        service.redeem(
            token="tok-1", email="invitee@example.com", client_key="client-1"
        )


# ---------------------------------------------------------------------------
# Audit: login, invite, redemption, and consent land on the recording sink
# without any token, link, or secret material.
# ---------------------------------------------------------------------------


def test_recording_sink_captures_login_invite_redemption_and_consent(
    tmp_path: Path,
) -> None:
    recorder = RecordingIdentityAuditSink()
    trail = IdentityAuditTrail(recorder, clock=_clock)

    trail.login(account_id=ACCOUNT_ID, actor_id=ACCOUNT_ID, client_key="203.0.113.7")
    trail.invite_created(
        invite_id="inv-2", actor_id=ACCOUNT_ID, email="Invitee@Example.com"
    )

    secret_token = "super-secret-invite-token-value"
    invites = LocalInviteStore(tmp_path / "invites.json")
    accounts = LocalAccountStore(tmp_path / "accounts.json")
    invites.put(_invite(invite_id="inv-2", token=secret_token))
    redemption = RedemptionService(
        invites, accounts, _clock, audit=RedemptionIdentityAuditSink(recorder)
    )
    result = redemption.redeem(
        token=secret_token, email="invitee@example.com", client_key="client-1"
    )
    trail.consent(account_id=result.account.id)

    events = recorder.events
    assert [event.kind for event in events] == [
        "login",
        "invite_created",
        "invite_redeemed",
        "consent",
    ]
    assert events[1].email == "invitee@example.com"
    assert events[2].outcome == "success"
    assert events[2].invite_id == "inv-2"

    rendered = repr(events)
    assert secret_token not in rendered
    assert invite_token_hash(secret_token) not in rendered
    assert "token=" not in rendered
    assert not any(hasattr(event, "token") for event in events)


def test_non_success_events_require_a_reason_and_success_forbids_one() -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        IdentityAuditEvent(kind="login", at=NOW, outcome="failure")
    with pytest.raises(ValueError, match="carries no reason"):
        IdentityAuditEvent(kind="login", at=NOW, reason="unexpected")


# ---------------------------------------------------------------------------
# Residency adapter: unified events map onto durable OperationAuditRecord.
# ---------------------------------------------------------------------------


class _FakeAuditWriter:
    """In-memory stand-in for the residency store's ``append_audit``."""

    def __init__(self) -> None:
        self.records: list[OperationAuditRecord] = []

    def append_audit(self, record: OperationAuditRecord) -> OperationAuditRecord:
        self.records.append(record)
        return record


def test_audit_events_never_accept_or_carry_a_secret_field() -> None:
    event = IdentityAuditEvent(
        kind="invite_created",
        at=NOW,
        actor_id=ACCOUNT_ID,
        invite_id="inv-9",
        email="invitee@example.com",
    )
    assert "token" not in event.as_dict()
    assert "link" not in event.as_dict()
    assert set(event.as_dict()) == {
        "accountId",
        "actorId",
        "at",
        "clientKey",
        "email",
        "inviteId",
        "kind",
        "outcome",
        "reason",
    }
