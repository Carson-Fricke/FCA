"""Server-side operator sessions and browser connection security."""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Literal, Protocol
from urllib.parse import urlsplit


class SessionConfigurationError(ValueError):
    """Raised when a runtime would expose an unsafe browser session boundary."""


class SessionSecurityError(PermissionError):
    """Raised when a browser connection cannot use an operator session."""


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().removesuffix(".")
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_origin(origin: str) -> tuple[str, str]:
    if not origin or origin != origin.strip() or "*" in origin:
        raise SessionConfigurationError("browser origins must be exact URLs")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query
        or parsed.fragment
    ):
        raise SessionConfigurationError("browser origins must be exact HTTP origins")
    try:
        port = parsed.port
    except ValueError as error:
        raise SessionConfigurationError("browser origin port is invalid") from error
    del port
    return parsed.scheme, parsed.hostname


@dataclass(frozen=True, slots=True)
class BrowserSecurity:
    """Browser controls applied to initial requests and reconnects."""

    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )
    csrf_protection: bool = True
    http_only_cookies: bool = True
    secure_cookies: bool = False
    same_site: Literal["strict", "lax", "none"] = "strict"

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise SessionConfigurationError("at least one browser origin is required")
        if len(self.allowed_origins) != len(set(self.allowed_origins)):
            raise SessionConfigurationError(
                "browser origins must not contain duplicates"
            )
        for origin in self.allowed_origins:
            _validate_origin(origin)


@dataclass(frozen=True, slots=True)
class RuntimeSecurity:
    """Runtime bind and authentication policy, safe for loopback by default."""

    bind_host: str = "127.0.0.1"
    tls_enabled: bool = False
    authentication: Literal["loopback", "entra"] = "loopback"
    browser: BrowserSecurity = BrowserSecurity()

    def __post_init__(self) -> None:
        if not self.bind_host or self.bind_host != self.bind_host.strip():
            raise SessionConfigurationError("bind_host must be a non-empty host")
        if not self.browser.csrf_protection:
            raise SessionConfigurationError("runtimes require CSRF protection")
        if not self.browser.http_only_cookies:
            raise SessionConfigurationError("runtimes require HttpOnly session cookies")
        if self.browser.same_site == "none":
            raise SessionConfigurationError(
                "runtimes require same-site browser cookies"
            )

        if self.is_loopback:
            for origin in self.browser.allowed_origins:
                _, origin_host = _validate_origin(origin)
                if not _is_loopback_host(origin_host):
                    raise SessionConfigurationError(
                        "loopback runtimes may allow only loopback browser origins"
                    )
            return

        if not self.tls_enabled:
            raise SessionConfigurationError("non-loopback runtimes require TLS")
        if self.authentication != "entra":
            raise SessionConfigurationError(
                "non-loopback runtimes require authenticated sessions"
            )
        if not self.browser.secure_cookies:
            raise SessionConfigurationError(
                "non-loopback runtimes require secure browser cookies"
            )
        for origin in self.browser.allowed_origins:
            scheme, _ = _validate_origin(origin)
            if scheme != "https":
                raise SessionConfigurationError(
                    "non-loopback browser origins must use HTTPS"
                )

    @property
    def is_loopback(self) -> bool:
        """Whether the server binds only to a local loopback interface."""
        return _is_loopback_host(self.bind_host)


@dataclass(frozen=True, slots=True)
class OperatorSession:
    """A server-side session bound to one operator and selected workspace."""

    session_id: str = field(repr=False)
    operator_id: str
    workspace_id: str
    authentication: Literal["loopback", "entra"]
    csrf_token: str = field(repr=False)
    created_at: datetime
    expires_at: datetime
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        for name in ("session_id", "operator_id", "workspace_id", "csrf_token"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and trimmed")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("operator sessions must expire after creation")

    def is_expired(self, now: datetime) -> bool:
        """Return whether the session is expired at an aware timestamp."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session checks require a timezone-aware timestamp")
        return now >= self.expires_at


class WorkspaceAccess(Protocol):
    """Authorization boundary consumed without importing workspace internals."""

    def authorize_operator(self, operator_id: str, workspace_id: str) -> None: ...

    def authorize_session(
        self, session: OperatorSession, requested_workspace_id: str
    ) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _random_token() -> str:
    return secrets.token_urlsafe(32)


class OperatorSessionService:
    """Issue and verify in-memory, server-side operator sessions."""

    def __init__(
        self,
        workspace_access: WorkspaceAccess,
        security: RuntimeSecurity | None = None,
        *,
        lifetime: timedelta = timedelta(hours=8),
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[], str] = _random_token,
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("session lifetime must be positive")
        self._workspace_access = workspace_access
        self._security = RuntimeSecurity() if security is None else security
        self._lifetime = lifetime
        self._clock = clock
        self._token_factory = token_factory
        self._sessions: dict[str, OperatorSession] = {}

    @property
    def security(self) -> RuntimeSecurity:
        """Return the validated runtime security policy."""
        return self._security

    def open_session(
        self,
        operator_id: str,
        workspace_id: str,
        *,
        authenticated_operator_id: str | None = None,
    ) -> OperatorSession:
        """Open a session after authenticating and authorizing its workspace."""
        if not self._security.is_loopback and authenticated_operator_id != operator_id:
            raise SessionSecurityError("authenticated operator identity required")
        self._workspace_access.authorize_operator(operator_id, workspace_id)

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session clock must return a timezone-aware timestamp")
        active_tokens = set(self._sessions)
        active_tokens.update(session.csrf_token for session in self._sessions.values())
        session_id = self._new_token(active_tokens)
        active_tokens.add(session_id)
        csrf_token = self._new_token(active_tokens)
        session = OperatorSession(
            session_id=session_id,
            operator_id=operator_id,
            workspace_id=workspace_id,
            authentication=self._security.authentication,
            csrf_token=csrf_token,
            created_at=now.astimezone(UTC),
            expires_at=(now + self._lifetime).astimezone(UTC),
        )
        self._sessions[session_id] = session
        return session

    def authorize_browser_connection(
        self,
        session_id: str,
        workspace_id: str,
        *,
        origin: str,
        csrf_token: str,
        transport_is_tls: bool,
        authenticated_operator_id: str | None = None,
    ) -> OperatorSession:
        """Authorize an initial browser request or reconnect to a workspace."""
        if origin not in self._security.browser.allowed_origins:
            raise SessionSecurityError("browser origin denied")
        if self._security.tls_enabled and not transport_is_tls:
            raise SessionSecurityError("TLS transport required")

        try:
            session = self._sessions[session_id]
        except KeyError as error:
            raise SessionSecurityError("operator session denied") from error
        if session.is_expired(self._clock()):
            del self._sessions[session_id]
            raise SessionSecurityError("operator session denied")
        if (
            not self._security.is_loopback
            and authenticated_operator_id != session.operator_id
        ):
            raise SessionSecurityError("authenticated operator identity required")
        if self._security.browser.csrf_protection and not compare_digest(
            session.csrf_token, csrf_token
        ):
            raise SessionSecurityError("CSRF verification failed")

        self._workspace_access.authorize_session(session, workspace_id)
        return session

    def revoke_session(self, session_id: str) -> None:
        """Revoke a server-side session without revealing whether it existed."""
        self._sessions.pop(session_id, None)

    def _new_token(self, existing: set[str]) -> str:
        for _ in range(3):
            token = self._token_factory()
            if token and token == token.strip() and token not in existing:
                return token
        raise RuntimeError("session token generator did not produce a unique token")


__all__ = [
    "BrowserSecurity",
    "OperatorSession",
    "OperatorSessionService",
    "RuntimeSecurity",
    "SessionConfigurationError",
    "SessionSecurityError",
    "WorkspaceAccess",
]
