"""Entra External ID OIDC verification for app-owned Thesis accounts.

This module owns the identity-provider boundary for the public Thesis release
described by ADR-001.  It validates Microsoft Entra External ID OIDC tokens and
performs the authorization-code + PKCE exchange, then returns the resolved
:class:`EntraIdentity`.  It deliberately does not touch accounts, sessions, or
cookies: resolving an app-owned account and issuing a server-side session are
later, separate steps.

The verifier is offline-testable by construction.  The JWKS client, the HTTP
client used for the token exchange, and the wall clock are all injected, so a
unit test can serve a local JWKS, answer the token endpoint with a mock
transport, and advance time deterministically.  No secret, client secret,
tenant value, or endpoint credential is hard-coded here; callers supply tenant,
audience, issuer, and endpoint URLs from deployment configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
import jwt
from jwt import PyJWK, PyJWKClient

_V2_ISSUER_TEMPLATE: Final = "https://login.microsoftonline.com/{tenant}/v2.0"
_JWKS_URL_TEMPLATE: Final = (
    "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
)
_TOKEN_URL_TEMPLATE: Final = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)

#: OIDC scopes requested by the authorization-code exchange.
_OPENID_SCOPES: Final = "openid profile email"


class EntraTokenError(ValueError):
    """A presented Entra token, or the code exchange, failed OIDC validation."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _claim_string(claims: Mapping[str, object], name: str) -> str | None:
    value = claims.get(name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _numeric_date(value: object) -> datetime | None:
    """Return a UTC instant for a NumericDate claim, or ``None`` when absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class EntraIdentity:
    """The verified identity claims extracted from an Entra ID token."""

    subject: str
    email: str | None = None
    display_name: str | None = None


def _identity_from_claims(claims: Mapping[str, object]) -> EntraIdentity:
    subject = _claim_string(claims, "sub") or _claim_string(claims, "oid")
    if subject is None:
        raise EntraTokenError("id_token is missing a subject claim")
    email = _claim_string(claims, "email")
    if email is None:
        preferred = _claim_string(claims, "preferred_username")
        if preferred is not None and "@" in preferred:
            email = preferred
    return EntraIdentity(
        subject=subject,
        email=email,
        display_name=_claim_string(claims, "name"),
    )


@dataclass(frozen=True, slots=True)
class EntraOidcVerifier:
    """Validates Entra External ID OIDC tokens for the Thesis API.

    ``tenant_id`` and ``audience`` are required.  ``issuer``, ``jwks_url``,
    ``token_url``, and ``client_id`` default to the standard v2 endpoints
    derived from the tenant; a real External ID tenant overrides them with its
    ``ciamlogin.com`` issuer, JWKS URL, and token URL.  ``client_id`` defaults
    to the API audience, matching the single app registration the deployment
    runbook creates.  ``leeway`` absorbs small clock skew, and ``clock`` lets a
    test pin "now" without hitting the network.

    Signature verification uses RS256 and the injected JWKS client; the
    configured issuer, audience, expiry, and -- when a nonce is supplied --
    the ``nonce`` claim are all enforced.  Failures raise :class:`EntraTokenError`.
    """

    tenant_id: str
    audience: str
    issuer: str | None = None
    jwks_url: str | None = None
    token_url: str | None = None
    client_id: str | None = None
    leeway: timedelta = timedelta(seconds=60)
    jwks_client: PyJWKClient | None = field(default=None, repr=False)
    http_client: httpx.Client | None = field(default=None, repr=False)
    clock: Callable[[], datetime] = field(default=_utc_now, repr=False)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "entra tenant id")
        _non_empty(self.audience, "entra audience")
        for name in ("issuer", "jwks_url", "token_url", "client_id"):
            value = getattr(self, name)
            if value is not None:
                _non_empty(value, name)
        if self.leeway < timedelta(0):
            raise ValueError("entra leeway must not be negative")

    @property
    def resolved_issuer(self) -> str:
        """Return the enforced v2 issuer for the configured tenant."""
        return self.issuer or _V2_ISSUER_TEMPLATE.format(tenant=self.tenant_id)

    @property
    def resolved_jwks_url(self) -> str:
        """Return the JWKS discovery URL for the configured tenant."""
        return self.jwks_url or _JWKS_URL_TEMPLATE.format(tenant=self.tenant_id)

    @property
    def resolved_token_url(self) -> str:
        """Return the OAuth2 token endpoint for the configured tenant."""
        return self.token_url or _TOKEN_URL_TEMPLATE.format(tenant=self.tenant_id)

    @property
    def resolved_client_id(self) -> str:
        """Return the client id used for the PKCE token exchange."""
        return self.client_id or self.audience

    def _signing_key(self, token: str) -> PyJWK:
        client = self.jwks_client
        if client is None:
            client = PyJWKClient(self.resolved_jwks_url)
            object.__setattr__(self, "jwks_client", client)
        return client.get_signing_key_from_jwt(token)

    def _validate_lifetime(self, claims: Mapping[str, object]) -> None:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise EntraTokenError("entra clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        expires_at = _numeric_date(claims.get("exp"))
        if expires_at is None:
            raise EntraTokenError("id_token is missing a valid expiry")
        if now > expires_at + self.leeway:
            raise EntraTokenError("id_token is expired")
        for name in ("nbf", "iat"):
            issued_at = _numeric_date(claims.get(name))
            if issued_at is not None and now + self.leeway < issued_at:
                raise EntraTokenError(f"id_token {name} is in the future")

    def validate_id_token(
        self, token: str, *, nonce: str | None = None
    ) -> EntraIdentity:
        """Return the verified identity for ``token``, or raise.

        The signature (RS256 via the tenant JWKS), v2 issuer, audience, and
        expiry are enforced.  When ``nonce`` is supplied it must equal the
        token's ``nonce`` claim, which binds the ID token to the browser's
        original authorization request.
        """
        if not isinstance(token, str) or not token.strip():
            raise EntraTokenError("id_token must be a non-empty string")
        try:
            signing_key = self._signing_key(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.resolved_issuer,
                options={
                    "require": ["exp"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as error:
            raise EntraTokenError("id_token failed Entra validation") from error
        self._validate_lifetime(claims)
        if nonce is not None and claims.get("nonce") != nonce:
            raise EntraTokenError("id_token nonce does not match")
        return _identity_from_claims(claims)

    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        redirect_uri: str,
        nonce: str | None = None,
    ) -> EntraIdentity:
        """Redeem an authorization code with PKCE and validate the ID token.

        The code is exchanged at the injected token endpoint with the SPA's
        ``client_id`` and the PKCE ``code_verifier``; the resulting ``id_token``
        is then validated exactly like a directly presented one, including the
        optional ``nonce`` binding.
        """
        _non_empty(code, "authorization code")
        _non_empty(code_verifier, "code verifier")
        _non_empty(redirect_uri, "redirect uri")
        data = {
            "grant_type": "authorization_code",
            "client_id": self.resolved_client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "scope": _OPENID_SCOPES,
        }
        response = self._post_token(data)
        if response.status_code >= 400:
            raise EntraTokenError("Entra token endpoint rejected the exchange")
        try:
            payload = response.json()
        except ValueError as error:
            raise EntraTokenError(
                "Entra token endpoint returned an invalid response"
            ) from error
        if not isinstance(payload, Mapping):
            raise EntraTokenError("Entra token endpoint returned an unexpected payload")
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token.strip():
            raise EntraTokenError("Entra token response is missing an id_token")
        return self.validate_id_token(id_token, nonce=nonce)

    def _post_token(self, data: Mapping[str, str]) -> httpx.Response:
        try:
            client = self.http_client
            if client is not None:
                return client.post(self.resolved_token_url, data=dict(data))
            with httpx.Client(timeout=10.0) as transient:
                return transient.post(self.resolved_token_url, data=dict(data))
        except httpx.HTTPError as error:
            raise EntraTokenError("Entra token endpoint is unreachable") from error


__all__ = [
    "EntraIdentity",
    "EntraOidcVerifier",
    "EntraTokenError",
]
