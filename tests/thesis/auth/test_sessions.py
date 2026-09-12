from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from thesis.auth import (
    BrowserSecurity,
    OperatorSessionService,
    RuntimeSecurity,
    SessionConfigurationError,
    SessionSecurityError,
    WorkspaceAuthorizationError,
    WorkspaceAuthorizer,
    WorkspaceOwnerLookup,
)
from thesis.identity import Session
from thesis.identity.local import LocalSessionStore


class MemorySessionStore:
    """In-memory test double for the durable identity ``SessionStore`` port."""

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


def _tokens() -> Iterator[str]:
    yield from ("session-one", "csrf-one", "session-two", "csrf-two")


def _owner_lookup(owners: dict[str, str]) -> WorkspaceOwnerLookup:
    def lookup(workspace_id: str) -> str | None:
        return owners.get(workspace_id)

    return lookup


def test_local_session_defaults_to_loopback_and_binds_owner_workspace() -> None:
    owners = {"workspace-a": "operator-a", "workspace-b": "operator-a"}
    tokens = _tokens()
    service = OperatorSessionService(
        WorkspaceAuthorizer(_owner_lookup(owners)),
        session_store=MemorySessionStore(),
        clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
        token_factory=lambda: next(tokens),
    )

    session = service.open_session("operator-a", "workspace-a")

    assert service.security.is_loopback
    assert service.security.bind_host == "127.0.0.1"
    assert session.operator_id == "operator-a"
    assert session.workspace_id == "workspace-a"
    assert session.authentication == "loopback"
    assert (
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="http://localhost:5173",
            csrf_token=session.csrf_token,
            transport_is_tls=False,
        )
        == session
    )

    with pytest.raises(WorkspaceAuthorizationError, match="access denied"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-b",
            origin="http://localhost:5173",
            csrf_token=session.csrf_token,
            transport_is_tls=False,
        )


def test_workspace_owner_is_checked_when_session_opens_and_reconnects() -> None:
    owners = {"workspace-a": "operator-a"}
    tokens = _tokens()
    service = OperatorSessionService(
        WorkspaceAuthorizer(_owner_lookup(owners)),
        session_store=MemorySessionStore(),
        token_factory=lambda: next(tokens),
    )

    with pytest.raises(WorkspaceAuthorizationError, match="access denied"):
        service.open_session("operator-b", "workspace-a")

    session = service.open_session("operator-a", "workspace-a")
    owners["workspace-a"] = "operator-b"
    with pytest.raises(WorkspaceAuthorizationError, match="access denied"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="http://127.0.0.1:5173",
            csrf_token=session.csrf_token,
            transport_is_tls=False,
        )


def test_browser_connection_checks_origin_csrf_expiry_and_revocation() -> None:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    tokens = _tokens()
    service = OperatorSessionService(
        WorkspaceAuthorizer(_owner_lookup({"workspace-a": "operator-a"})),
        lifetime=timedelta(minutes=5),
        session_store=MemorySessionStore(),
        clock=lambda: now,
        token_factory=lambda: next(tokens),
    )
    session = service.open_session("operator-a", "workspace-a")

    with pytest.raises(SessionSecurityError, match="origin"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="http://malicious.example",
            csrf_token=session.csrf_token,
            transport_is_tls=False,
        )
    with pytest.raises(SessionSecurityError, match="CSRF"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="http://localhost:5173",
            csrf_token="incorrect",
            transport_is_tls=False,
        )

    now += timedelta(minutes=5)
    with pytest.raises(SessionSecurityError, match="session denied"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="http://localhost:5173",
            csrf_token=session.csrf_token,
            transport_is_tls=False,
        )

    second = service.open_session("operator-a", "workspace-a")
    service.revoke_session(second.session_id)
    with pytest.raises(SessionSecurityError, match="session denied"):
        service.authorize_browser_connection(
            second.session_id,
            "workspace-a",
            origin="http://localhost:5173",
            csrf_token=second.csrf_token,
            transport_is_tls=False,
        )


@pytest.mark.parametrize(
    "security",
    [
        RuntimeSecurity(
            bind_host="0.0.0.0",
            tls_enabled=True,
            authentication="entra",
            browser=BrowserSecurity(
                allowed_origins=("https://thesis.example.invalid",),
                csrf_protection=True,
                secure_cookies=True,
            ),
        ),
    ],
)
def test_valid_non_loopback_session_requires_authentication_and_tls(
    security: RuntimeSecurity,
) -> None:
    tokens = _tokens()
    service = OperatorSessionService(
        WorkspaceAuthorizer(_owner_lookup({"workspace-a": "operator-a"})),
        security,
        session_store=MemorySessionStore(),
        token_factory=lambda: next(tokens),
    )
    with pytest.raises(SessionSecurityError, match="authenticated"):
        service.open_session("operator-a", "workspace-a")

    session = service.open_session(
        "operator-a",
        "workspace-a",
        authenticated_operator_id="operator-a",
    )
    with pytest.raises(SessionSecurityError, match="TLS"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="https://thesis.example.invalid",
            csrf_token=session.csrf_token,
            transport_is_tls=False,
            authenticated_operator_id="operator-a",
        )
    with pytest.raises(SessionSecurityError, match="authenticated"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="https://thesis.example.invalid",
            csrf_token=session.csrf_token,
            transport_is_tls=True,
        )
    with pytest.raises(SessionSecurityError, match="authenticated operator"):
        service.authorize_browser_connection(
            session.session_id,
            "workspace-a",
            origin="https://thesis.example.invalid",
            csrf_token=session.csrf_token,
            transport_is_tls=True,
            authenticated_operator_id="operator-b",
        )
    assert service.authorize_browser_connection(
        session.session_id,
        "workspace-a",
        origin="https://thesis.example.invalid",
        csrf_token=session.csrf_token,
        transport_is_tls=True,
        authenticated_operator_id="operator-a",
    )


@pytest.mark.parametrize(
    ("tls", "authentication", "browser", "message"),
    [
        (
            False,
            "entra",
            BrowserSecurity(
                allowed_origins=("https://thesis.example.invalid",),
                secure_cookies=True,
            ),
            "TLS",
        ),
        (
            True,
            "loopback",
            BrowserSecurity(
                allowed_origins=("https://thesis.example.invalid",),
                secure_cookies=True,
            ),
            "authenticated",
        ),
        (
            True,
            "entra",
            BrowserSecurity(
                allowed_origins=("https://thesis.example.invalid",),
                secure_cookies=False,
            ),
            "secure browser cookies",
        ),
        (
            True,
            "entra",
            BrowserSecurity(
                allowed_origins=("https://thesis.example.invalid",),
                csrf_protection=False,
                secure_cookies=True,
            ),
            "CSRF",
        ),
        (
            True,
            "entra",
            BrowserSecurity(
                allowed_origins=("https://thesis.example.invalid",),
                http_only_cookies=False,
                secure_cookies=True,
            ),
            "HttpOnly",
        ),
        (
            True,
            "entra",
            BrowserSecurity(
                allowed_origins=("http://thesis.example.invalid",),
                secure_cookies=True,
            ),
            "HTTPS",
        ),
    ],
)
def test_non_loopback_configuration_fails_closed(
    tls: bool,
    authentication: Literal["loopback", "entra"],
    browser: BrowserSecurity,
    message: str,
) -> None:
    with pytest.raises(SessionConfigurationError, match=message):
        RuntimeSecurity(
            bind_host="0.0.0.0",
            tls_enabled=tls,
            authentication=authentication,
            browser=browser,
        )


def test_loopback_policy_rejects_remote_or_wildcard_browser_origins() -> None:
    with pytest.raises(SessionConfigurationError, match="loopback browser origins"):
        RuntimeSecurity(
            browser=BrowserSecurity(
                allowed_origins=("http://thesis.example.invalid:5173",)
            )
        )
    with pytest.raises(SessionConfigurationError, match="exact URLs"):
        BrowserSecurity(allowed_origins=("http://*.example.invalid",))


def test_session_survives_a_service_restart(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    owners = {"workspace-a": "operator-a"}
    store_path = tmp_path / "sessions.json"
    tokens = _tokens()

    first = OperatorSessionService(
        WorkspaceAuthorizer(_owner_lookup(owners)),
        session_store=LocalSessionStore(store_path),
        clock=lambda: now,
        token_factory=lambda: next(tokens),
    )
    session = first.open_session("operator-a", "workspace-a")

    # A fresh service over the same on-disk store must resolve the session.
    restarted = OperatorSessionService(
        WorkspaceAuthorizer(_owner_lookup(owners)),
        session_store=LocalSessionStore(store_path),
        clock=lambda: now,
    )
    resolved = restarted.authorize_browser_connection(
        session.session_id,
        "workspace-a",
        origin="http://localhost:5173",
        csrf_token=session.csrf_token,
        transport_is_tls=False,
    )

    assert resolved == session
    assert resolved.operator_id == "operator-a"
    assert resolved.workspace_id == "workspace-a"
    assert resolved.csrf_token == session.csrf_token
