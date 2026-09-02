"""Authentication dependencies for the market-lake HTTP API."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "unauthorized",
            "message": "a valid bearer token is required",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


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
