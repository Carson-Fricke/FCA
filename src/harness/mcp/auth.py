"""Bearer credential resolution for the harness MCP server.

The MCP server is a client of the remote quant-harness API, so the one
credential it ever moves is that API's bearer token. The documented choice is
the ``HARNESS_API_BEARER_TOKEN`` environment variable: it is the deployment
variable the harness CLI (`api --config`), the HTTP boundary tests, and the
``harness`` runtime image already standardize on. ``THESIS_API__BEARER_TOKEN``
is deliberately not consulted — the MCP server is part of the harness product,
not a Thesis component, and one variable per deployment keeps composition
unambiguous.

The token is resolved from the environment at request time and is never
stored on the server or in any serialized server state. The only serialized
trace of the credential is the variable's *name* plus a boolean
``bearer_configured`` flag; see :meth:`harness.mcp.tools.HarnessToolsContext.as_settings`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

BEARER_TOKEN_VARIABLE: Final = "HARNESS_API_BEARER_TOKEN"

_AUTHORIZATION_SCHEME: Final = "Bearer"


class MissingBearerCredentialError(RuntimeError):
    """Raised when a remote call is attempted without a resolvable bearer."""


class BearerTokenSource(Protocol):
    """Credential surface the tool handlers compose against.

    Implementations resolve the token at call time; nothing about the token
    may be retained between calls.
    """

    def bearer_token(self) -> str | None: ...

    def bearer_headers(self) -> dict[str, str]: ...


def bearer_token_from_environ(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the stripped bearer token, or ``None`` when unconfigured."""
    values = os.environ if environ is None else environ
    token = values.get(BEARER_TOKEN_VARIABLE)
    if token is None or not token.strip():
        return None
    return token.strip()


def bearer_headers(token: str | None) -> dict[str, str]:
    """Build the ``Authorization`` header for one remote harness API call."""
    if not isinstance(token, str) or not token.strip():
        raise MissingBearerCredentialError(
            f"a non-empty {BEARER_TOKEN_VARIABLE} is required for remote "
            "harness API calls"
        )
    return {"Authorization": f"{_AUTHORIZATION_SCHEME} {token.strip()}"}


@dataclass(frozen=True, slots=True)
class EnvironmentBearerTokenSource:
    """Resolve the bearer token from the deployment environment per call."""

    environ: Mapping[str, str] | None = None

    def bearer_token(self) -> str | None:
        """Return the current token without retaining it."""
        return bearer_token_from_environ(self.environ)

    def bearer_headers(self) -> dict[str, str]:
        """Return the request headers, or raise when unconfigured."""
        return bearer_headers(self.bearer_token())


__all__ = [
    "BEARER_TOKEN_VARIABLE",
    "BearerTokenSource",
    "EnvironmentBearerTokenSource",
    "MissingBearerCredentialError",
    "bearer_headers",
    "bearer_token_from_environ",
]
