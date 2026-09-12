"""Deterministic tests for issuer-side session cookies, CSRF, and revocation.

The suite composes :func:`thesis.http.application.create_app` with an in-memory
account directory, a fake Entra verifier, an in-memory session store, and a
fixed clock.  ``TestClient`` runs over an HTTPS base URL so the ``Secure``
session cookie is echoed back exactly as a browser would on a TLS connection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from thesis.auth import (
    BrowserSecurity,
    OperatorSessionService,
    RuntimeSecurity,
    WorkspaceAuthorizer,
)
from thesis.http.application import create_app
from thesis.http.auth import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from thesis.identity.entra import EntraIdentity, EntraTokenError
from thesis.identity.models import Account, OAuthSubject, Session

TOKEN = "thesis-test-token"
WORKSPACE = "workspace-a"
ACCOUNT_ID = "acct-1"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

_CHAT_URL = f"/v1/workspaces/{WORKSPACE}/chats"
_CHAT_BODY = {"title": "Research", "owner_actor_id": "operator"}


class _FakeAccountStore:
    """In-memory :class:`~thesis.identity.ports.AccountStore` double."""

    def __init__(self, accounts: tuple[Account, ...] = ()) -> None:
        self._accounts = {account.id: account for account in accounts}

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def get_by_email(self, email: str) -> Account | None:
        wanted = email.strip().lower()
        for account in self._accounts.values():
            if account.email == wanted:
                return account
        return None

    def put(self, account: Account) -> Account:
        self._accounts[account.id] = account
        return account

    def list(self) -> tuple[Account, ...]:
        return tuple(self._accounts.values())


class _FakeVerifier:
    """Offline stand-in for :class:`~thesis.identity.entra.EntraOidcVerifier`."""

    def validate_id_token(
        self, token: str, *, nonce: str | None = None
    ) -> EntraIdentity:
        del nonce
        if token != "valid-id-token":
            raise EntraTokenError("id_token failed Entra validation")
        return EntraIdentity(
            subject="subject-1",
            email="owner@example.com",
            display_name="Owner",
        )

    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        redirect_uri: str,
        nonce: str | None = None,
    ) -> EntraIdentity:
        del code, code_verifier, redirect_uri, nonce
        raise EntraTokenError("code exchange is not exercised")


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


def _account() -> Account:
    return Account(
        id=ACCOUNT_ID,
        email="owner@example.com",
        display_name="Owner",
        oauth_subjects=(OAuthSubject(provider="entra", subject="subject-1"),),
        role="owner",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _token_factory() -> Iterator[str]:
    yield from ("tok-0001", "csrf-0001", "tok-0002", "csrf-0002")


def _service(
    store: _MemorySessionStore,
    clock: Callable[[], datetime],
    *,
    lifetime: timedelta = timedelta(hours=8),
) -> OperatorSessionService:
    tokens = _token_factory()
    return OperatorSessionService(
        WorkspaceAuthorizer(
            lambda workspace_id: ACCOUNT_ID if workspace_id == WORKSPACE else None
        ),
        RuntimeSecurity(browser=BrowserSecurity(secure_cookies=True, same_site="lax")),
        session_store=store,
        lifetime=lifetime,
        clock=clock,
        token_factory=lambda: next(tokens),
    )


def _client(
    store: _MemorySessionStore,
    service: OperatorSessionService,
    clock: Callable[[], datetime],
    *,
    with_sessions: bool = True,
) -> TestClient:
    app = create_app(
        bearer_token=TOKEN,
        account_store=_FakeAccountStore((_account(),)),
        auth_verifier=_FakeVerifier(),
        session_service=service if with_sessions else None,
        session_store=store if with_sessions else None,
        session_workspace_id=WORKSPACE if with_sessions else None,
        session_clock=clock,
    )
    return TestClient(app, base_url="https://testserver")


def _login(client: TestClient) -> dict[str, object]:
    response = client.post("/v1/auth/login", json={"id_token": "valid-id-token"})
    assert response.status_code == 200, response.text
    return response.json()


def test_login_sets_secure_httponly_lax_cookie_and_returns_csrf_token() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)

    response = client.post("/v1/auth/login", json={"id_token": "valid-id-token"})

    assert response.status_code == 200
    assert response.json()["csrf_token"] == "csrf-0001"
    header = response.headers["set-cookie"]
    lowered = header.lower()
    assert lowered.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert client.cookies.get(SESSION_COOKIE_NAME) == f"{WORKSPACE}:tok-0001"


def test_login_route_is_reachable_without_credentials() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)

    response = client.post("/v1/auth/login", json={"id_token": "valid-id-token"})

    assert response.status_code == 200
    assert "Authorization" not in response.request.headers


def test_state_changing_request_requires_the_bound_csrf_token() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)
    csrf_token = _login(client)["csrf_token"]

    rejected = client.post(_CHAT_URL, json=_CHAT_BODY)

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "csrf_failed"

    accepted = client.post(
        _CHAT_URL, json=_CHAT_BODY, headers={CSRF_HEADER_NAME: str(csrf_token)}
    )
    assert accepted.status_code == 200


def test_session_cookie_authenticates_safe_routes_and_bearer_still_works() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)

    assert client.get("/v1/auth/check").status_code == 401

    _login(client)
    assert client.get("/v1/auth/check").status_code == 200

    anonymous = TestClient(client.app, base_url="https://testserver")
    assert (
        anonymous.get(
            "/v1/auth/check", headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code
        == 200
    )
    assert (
        anonymous.get(
            "/v1/auth/check", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_logout_revokes_the_server_side_session_and_clears_the_cookie() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)
    csrf_token = _login(client)["csrf_token"]
    session_id = str(client.cookies.get(SESSION_COOKIE_NAME))
    assert store.get(session_id) is not None

    response = client.post(
        "/v1/auth/logout", headers={CSRF_HEADER_NAME: str(csrf_token)}
    )

    assert response.status_code == 200
    record = store.get(session_id)
    assert record is not None
    assert record.revoked_at is not None
    assert client.cookies.get(SESSION_COOKIE_NAME) is None
    assert client.get("/v1/auth/check").status_code == 401


def test_revoked_session_cookie_is_rejected_on_subsequent_requests() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    service = _service(store, clock)
    client = _client(store, service, clock)
    _login(client)
    session_id = str(client.cookies.get(SESSION_COOKIE_NAME))

    service.revoke_session(session_id)

    assert client.get("/v1/auth/check").status_code == 401


def test_expired_session_cookie_is_rejected_on_subsequent_requests() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    service = _service(store, clock, lifetime=timedelta(minutes=5))
    client = _client(store, service, clock)
    _login(client)
    assert client.get("/v1/auth/check").status_code == 200

    clock.now = NOW + timedelta(minutes=6)

    assert client.get("/v1/auth/check").status_code == 401


def test_revoke_endpoint_invalidates_an_owned_session() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)
    csrf_token = _login(client)["csrf_token"]
    session_id = str(client.cookies.get(SESSION_COOKIE_NAME))

    revoked = client.post(
        "/v1/auth/revoke",
        headers={CSRF_HEADER_NAME: str(csrf_token)},
        json={"session_id": session_id},
    )

    assert revoked.status_code == 200
    record = store.get(session_id)
    assert record is not None
    assert record.revoked_at is not None
    assert client.get("/v1/auth/check").status_code == 401


def test_unauthenticated_state_change_is_rejected_without_csrf_cookie() -> None:
    store = _MemorySessionStore()
    clock = _MutableClock(NOW)
    client = _client(store, _service(store, clock), clock)

    response = client.post(_CHAT_URL, json=_CHAT_BODY)

    assert response.status_code == 401
