"""Entra OIDC login, server-side session cookies, and CSRF protection.

The router exposes ``POST /v1/auth/login``.  It accepts either an Entra
authorization code (exchanged with PKCE at the injected token endpoint) or an
already-issued ID token, verifies the identity through
:class:`~thesis.identity.entra.EntraOidcVerifier`, resolves an app-owned account
in the injected :class:`~thesis.identity.ports.AccountStore`, and — when a
session service is configured — opens a durable server-side operator session
through :class:`~thesis.auth.session.OperatorSessionService`.

Account resolution is invite-only by construction: the identity is looked up by
normalized email first and then by a linked Entra OAuth subject; when no
account matches, the request is rejected.

Session cookie and CSRF design
------------------------------

* The durable session id is delivered in the ``thesis_session`` cookie.  The
  cookie is ``HttpOnly``, ``Secure``, and ``SameSite=Lax``; the flags are taken
  from the :class:`~thesis.auth.session.BrowserSecurity` policy carried by the
  injected :class:`~thesis.auth.session.OperatorSessionService`, so cookie and
  runtime policy stay in one place.
* CSRF uses the explicit-header pattern.  A successful login returns the
  session's CSRF secret as ``csrf_token`` in the JSON body and the browser
  echoes it in the ``X-CSRF-Token`` header on every state-changing request.
  The guard compares the header against the server-side session record with a
  constant-time comparison; safe methods (GET/HEAD/OPTIONS/TRACE) are exempt,
  and requests that carry no session cookie (pure bearer/Entra clients) are not
  subject to CSRF because they carry no ambient cookie authority.
* ``POST /v1/auth/logout`` revokes the current session server-side and clears
  the cookie; ``POST /v1/auth/revoke`` revokes one session owned by the calling
  operator.  Both require a valid session cookie and a matching CSRF token.

The router performs its own OIDC validation, so it must not be registered with
the shared bearer/Entra dependency: login is the credential-establishing
endpoint.  The session-aware dependency here composes with, rather than
replaces, the application's bearer/Entra authenticator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from typing import Annotated, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from thesis.auth.session import (
    BrowserSecurity,
    OperatorSession,
    OperatorSessionService,
)
from thesis.identity.entra import EntraIdentity, EntraTokenError
from thesis.identity.models import Account, Session, normalize_email
from thesis.identity.ports import AccountStore, SessionStore

#: Name of the durable server-side session cookie issued at login.
SESSION_COOKIE_NAME = "thesis_session"
#: Header carrying the CSRF secret bound to the session record.
CSRF_HEADER_NAME = "X-CSRF-Token"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_bearer_scheme = HTTPBearer(auto_error=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    moment = clock()
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("session clock must return a timezone-aware timestamp")
    return moment


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "unauthorized",
            "message": "a valid session cookie or bearer token is required",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _csrf_rejected() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "csrf_failed",
            "message": "a valid CSRF token is required for this request",
        },
    )


def _load_active_session(
    session_store: SessionStore, session_id: str | None, now: datetime
) -> Session | None:
    """Return the active session record for ``session_id``, or ``None``.

    A session is active only while it is neither revoked nor expired; both
    checks are anchored to caller-supplied, timezone-aware time.
    """
    if not session_id:
        return None
    record = session_store.get(session_id)
    if record is None:
        return None
    return record if record.is_active(now) else None


class _BearerAuthenticator(Protocol):
    """The application bearer/Entra authenticator this dependency composes.

    The authenticator is a FastAPI dependency, so it receives the request
    alongside the extracted bearer credentials and may store the resolved
    principal on ``request.state`` for downstream routes.
    """

    async def __call__(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class SessionCookieAuthenticator:
    """Authenticate a request from the server-side session cookie.

    The cookie is tried first; when it is absent the request is delegated
    unchanged to the injected bearer/Entra authenticator.  A present-but-invalid
    cookie fails closed instead of silently falling back, so a revoked or
    expired session is never masked by a public route.
    """

    session_store: SessionStore
    fallback: _BearerAuthenticator | None = None
    clock: Callable[[], datetime] = _utc_now
    cookie_name: str = SESSION_COOKIE_NAME

    async def __call__(
        self,
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
        ],
    ) -> str:
        """Return the authenticated principal or raise the shared 401.

        The principal is also stored on the request state so downstream routes
        attribute actions to the authenticated operator.
        """
        raw = request.cookies.get(self.cookie_name)
        if raw:
            record = _load_active_session(
                self.session_store, raw, _aware_now(self.clock)
            )
            if record is None:
                raise _unauthorized()
            principal = f"session:{record.account_id}"
            request.state.authenticated_principal = principal
            return principal
        if self.fallback is None:
            raise _unauthorized()
        principal = await self.fallback(request, credentials)
        request.state.authenticated_principal = principal
        return principal


@dataclass(frozen=True, slots=True)
class CsrfTokenGuard:
    """Reject state-changing cookie requests lacking the bound CSRF token.

    Safe methods are exempt, and requests that carry no session cookie are left
    to the bearer/Entra authenticator: they present no ambient authority and so
    are not exposed to CSRF.
    """

    session_store: SessionStore
    clock: Callable[[], datetime] = _utc_now
    cookie_name: str = SESSION_COOKIE_NAME
    header_name: str = CSRF_HEADER_NAME

    def __call__(self, request: Request) -> None:
        if request.method.upper() in _SAFE_METHODS:
            return
        raw = request.cookies.get(self.cookie_name)
        if not raw:
            return
        record = _load_active_session(self.session_store, raw, _aware_now(self.clock))
        if record is None:
            raise _unauthorized()
        presented = request.headers.get(self.header_name)
        if presented is None or not compare_digest(presented, record.csrf_secret):
            raise _csrf_rejected()


@dataclass(frozen=True, slots=True)
class _SessionCookieDependency:
    """Resolve the active session record, or raise the shared 401."""

    session_store: SessionStore
    clock: Callable[[], datetime] = _utc_now
    cookie_name: str = SESSION_COOKIE_NAME

    def __call__(self, request: Request) -> Session:
        raw = request.cookies.get(self.cookie_name)
        record = (
            _load_active_session(self.session_store, raw, _aware_now(self.clock))
            if raw
            else None
        )
        if record is None:
            raise _unauthorized()
        return record


class AuthLoginRequest(BaseModel):
    """One Entra login result: an authorization code or an ID token."""

    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    id_token: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None
    nonce: str | None = None

    @model_validator(mode="after")
    def _one_credential_with_pkce_inputs(self) -> AuthLoginRequest:
        provided = (self.code is not None, self.id_token is not None)
        if sum(provided) != 1:
            raise ValueError("provide exactly one of code or id_token")
        if self.code is not None and (
            self.code_verifier is None or self.redirect_uri is None
        ):
            raise ValueError("code exchange requires code_verifier and redirect_uri")
        return self


class RevokeSessionRequest(BaseModel):
    """The session to revoke on behalf of the calling operator."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)


def _account_payload(account: Account) -> dict[str, object]:
    """Shape one account for the login response (identity only, no session)."""
    return {
        "id": account.id,
        "email": account.email,
        "display_name": account.display_name,
        "role": account.role,
        "status": account.status,
        "oauth_subjects": [
            {"provider": link.provider, "subject": link.subject}
            for link in account.oauth_subjects
        ],
    }


def _resolve_account(store: AccountStore, identity: EntraIdentity) -> Account | None:
    """Return the app-owned account for ``identity``, or ``None``.

    A normalized-email match is authoritative; otherwise an account that has
    already linked this Entra subject is returned.  An identity with neither is
    unprovisioned and must be rejected by the caller.
    """
    if identity.email is not None:
        try:
            normalized = normalize_email(identity.email)
        except ValueError:
            normalized = None
        if normalized is not None:
            account = store.get_by_email(normalized)
            if account is not None:
                return account
    for account in store.list():
        for link in account.oauth_subjects:
            if link.provider == "entra" and link.subject == identity.subject:
                return account
    return None


def _set_session_cookie(
    response: Response, session: OperatorSession, browser: BrowserSecurity
) -> None:
    """Attach the session cookie using the validated browser policy."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.session_id,
        max_age=int((session.expires_at - session.created_at).total_seconds()),
        path="/",
        secure=browser.secure_cookies,
        httponly=browser.http_only_cookies,
        samesite=browser.same_site,
    )


def _clear_session_cookie(response: Response, browser: BrowserSecurity) -> None:
    """Expire the session cookie with flags matching its issuance."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=browser.secure_cookies,
        httponly=browser.http_only_cookies,
        samesite=browser.same_site,
    )


@runtime_checkable
class OidcVerifier(Protocol):
    """Structural OIDC verifier so tests and alternate providers can inject."""

    def validate_id_token(
        self, token: str, *, nonce: str | None = None
    ) -> EntraIdentity: ...

    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        redirect_uri: str,
        nonce: str | None = None,
    ) -> EntraIdentity: ...


def create_auth_router(
    account_store: AccountStore,
    verifier: OidcVerifier,
    *,
    session_service: OperatorSessionService | None = None,
    session_store: SessionStore | None = None,
    workspace_id: str | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> APIRouter:
    """Create the Entra login router over the injected store and verifier.

    The router is intentionally not protected by the shared bearer/Entra
    dependency: login is the credential-establishing endpoint.  When a session
    service and its store are supplied the successful login also opens a
    server-side session, sets the secure session cookie, and returns the CSRF
    token; ``logout`` and ``revoke`` are registered to invalidate sessions
    server-side.  Without a session service the router keeps the identity-only
    behavior and issues no cookie of any kind.
    """
    if (session_service is None) != (session_store is None):
        raise ValueError(
            "session_service and session_store must be configured together"
        )
    if session_service is not None and (
        workspace_id is None or not workspace_id.strip()
    ):
        raise ValueError("a session workspace_id is required to issue sessions")

    browser: BrowserSecurity | None = None
    if session_service is not None:
        browser = session_service.security.browser
        if not browser.http_only_cookies:
            raise ValueError("session cookies must be HttpOnly")
        if not browser.secure_cookies:
            raise ValueError("session cookies must be Secure")
        if browser.same_site == "none":
            raise ValueError("session cookies must be SameSite")

    router = APIRouter(prefix="/v1/auth", tags=["auth"])
    csrf_guard = (
        CsrfTokenGuard(session_store=session_store, clock=clock)
        if session_store is not None
        else None
    )
    session_dependency = (
        _SessionCookieDependency(session_store=session_store, clock=clock)
        if session_store is not None
        else None
    )

    @router.post("/login")
    async def login(payload: AuthLoginRequest, response: Response) -> dict[str, object]:
        try:
            if payload.code is not None:
                if payload.code_verifier is None or payload.redirect_uri is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "code": "invalid_request",
                            "message": "code exchange requires code_verifier and redirect_uri",
                        },
                    )
                identity = verifier.exchange_code(
                    payload.code,
                    code_verifier=payload.code_verifier,
                    redirect_uri=payload.redirect_uri,
                    nonce=payload.nonce,
                )
            else:
                identity = verifier.validate_id_token(
                    payload.id_token or "", nonce=payload.nonce
                )
        except EntraTokenError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "invalid_token",
                    "message": "the Entra sign-in could not be validated",
                },
            ) from error

        account = _resolve_account(account_store, identity)
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "account_not_found",
                    "message": "no Thesis account is provisioned for this identity",
                },
            )
        result: dict[str, object] = {"account": _account_payload(account)}
        if session_service is not None and browser is not None:
            session = session_service.open_session(
                account.id,
                workspace_id or "",
                authenticated_operator_id=account.id,
            )
            _set_session_cookie(response, session, browser)
            result["csrf_token"] = session.csrf_token
        return result

    if (
        session_service is not None
        and session_store is not None
        and browser is not None
        and csrf_guard is not None
        and session_dependency is not None
    ):
        service = session_service
        store = session_store
        browser_policy = browser
        guard = csrf_guard
        session_cookie = session_dependency

        def _current_record(request: Request) -> Session:
            record = _load_active_session(
                store,
                request.cookies.get(SESSION_COOKIE_NAME),
                _aware_now(clock),
            )
            if record is None:
                raise _unauthorized()
            return record

        @router.post(
            "/logout",
            dependencies=[Depends(guard), Depends(session_cookie)],
        )
        async def logout(response: Response, request: Request) -> dict[str, str]:
            """Revoke the current session server-side and clear the cookie."""
            service.revoke_session(_current_record(request).id)
            _clear_session_cookie(response, browser_policy)
            return {"status": "ok"}

        @router.post(
            "/revoke",
            dependencies=[Depends(guard), Depends(session_cookie)],
        )
        async def revoke(
            payload: RevokeSessionRequest, request: Request
        ) -> dict[str, str]:
            """Revoke one session owned by the calling operator."""
            record = _current_record(request)
            target = store.get(payload.session_id)
            if target is None or target.account_id != record.account_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "session_not_found",
                        "message": "no revocable session was found",
                    },
                )
            service.revoke_session(payload.session_id)
            return {"status": "ok"}

    return router


__all__ = [
    "CSRF_HEADER_NAME",
    "SESSION_COOKIE_NAME",
    "AuthLoginRequest",
    "CsrfTokenGuard",
    "OidcVerifier",
    "RevokeSessionRequest",
    "SessionCookieAuthenticator",
    "create_auth_router",
]
