"""Authentication dependencies for the market-lake HTTP API."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from secrets import compare_digest
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK, PyJWKClient

bearer_scheme = HTTPBearer(auto_error=False)

_V1_ISSUER_TEMPLATE = "https://sts.windows.net/{tenant}/"
_V2_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant}/v2.0"
_JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "unauthorized",
            "message": "a valid bearer token is required",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def authenticate_bearer(
    credentials: HTTPAuthorizationCredentials | None,
    expected_token: str | None,
) -> str:
    """Validate one configured bearer token using constant-time comparison."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    if expected_token is None or not compare_digest(
        credentials.credentials, expected_token
    ):
        raise _unauthorized()

    return credentials.credentials


def _looks_like_jwt(credential: str) -> bool:
    segments = credential.split(".")
    return len(segments) == 3 and all(segments)


@dataclass(frozen=True, slots=True)
class EntraJwtAuthenticator:
    """Validates Entra managed-identity JWTs for the configured application.

    Signature verification uses the tenant JWKS discovery document, the
    audience must equal the lake application client ID, both the v1
    ``sts.windows.net`` issuer form (minted for managed identities) and the v2
    ``login.microsoftonline.com`` form are accepted, and the configured
    application role must appear in the ``roles`` claim.
    """

    tenant_id: str
    audience: str
    role: str = "Lake.Access"
    jwks_client: PyJWKClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "entra tenant id")
        _non_empty(self.audience, "entra audience")
        _non_empty(self.role, "entra role")

    @property
    def _issuers(self) -> Sequence[str]:
        return (
            _V1_ISSUER_TEMPLATE.format(tenant=self.tenant_id),
            _V2_ISSUER_TEMPLATE.format(tenant=self.tenant_id),
        )

    def _signing_key(self, token: str) -> PyJWK:
        client = self.jwks_client
        if client is None:
            client = PyJWKClient(_JWKS_URL_TEMPLATE.format(tenant=self.tenant_id))
            object.__setattr__(self, "jwks_client", client)
        return client.get_signing_key_from_jwt(token)

    def authenticate(self, token: str) -> str:
        """Return the presented token after Entra validation, or raise 401."""
        try:
            signing_key = self._signing_key(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=list(self._issuers),
                options={"require": ["exp"]},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as error:
            raise _unauthorized() from error
        roles = claims.get("roles", ())
        if isinstance(roles, str):
            roles = (roles,)
        if self.role not in roles:
            raise _unauthorized()
        return token


@dataclass(frozen=True, slots=True)
class BearerAuthenticator:
    """FastAPI dependency that validates a single configured bearer token."""

    expected_token: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if self.expected_token is not None and not self.expected_token.strip():
            raise ValueError("bearer token must be a non-empty string")

    async def __call__(
        self,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
        ],
    ) -> str:
        """Return the presented token or raise a uniform authentication error."""
        return authenticate_bearer(credentials, self.expected_token)


@dataclass(frozen=True, slots=True)
class CombinedAuthenticator:
    """FastAPI dependency accepting static bearer tokens and Entra JWTs.

    Without Entra configuration this dependency behaves exactly like
    :class:`BearerAuthenticator`. With Entra configuration a three-segment JWT
    credential is validated as an Entra token while any other bearer string
    continues to validate against the static token; invalid Entra tokens fail
    closed with the shared 401 error shape.
    """

    expected_token: str | None = field(repr=False)
    entra: EntraJwtAuthenticator | None = None

    def __post_init__(self) -> None:
        if self.expected_token is not None and not self.expected_token.strip():
            raise ValueError("bearer token must be a non-empty string")

    async def __call__(
        self,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
        ],
    ) -> str:
        """Return the presented token or raise a uniform authentication error."""
        if self.entra is None:
            return authenticate_bearer(credentials, self.expected_token)
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized()
        credential = credentials.credentials
        if _looks_like_jwt(credential):
            return self.entra.authenticate(credential)
        return authenticate_bearer(credentials, self.expected_token)


__all__ = [
    "BearerAuthenticator",
    "CombinedAuthenticator",
    "EntraJwtAuthenticator",
    "authenticate_bearer",
    "bearer_scheme",
]
