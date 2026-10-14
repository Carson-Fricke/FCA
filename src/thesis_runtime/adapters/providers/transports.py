"""Concrete local transports for model providers (network lives here).

Providers own no network client: they call an injected transport.  These are
the workstation/local adapters that satisfy those ports with ``httpx`` and the
process environment, keeping credentials out of provider and agent code.
"""

from __future__ import annotations

import math
import os
from collections.abc import Generator, Mapping
from contextlib import closing
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from thesis_runtime.adapters.providers.invocations import ProviderUnreachableError
from thesis_runtime.adapters.providers.streaming import sse_data
from thesis_runtime.contracts.errors import ProviderRejectedError


class HttpOpenRouterTransport:
    """OpenRouter transport over ``httpx``; the only module that opens sockets."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        # Build (including JSON serialization) outside the network error boundary.
        request = self._client.build_request(
            "POST",
            f"{self._base_url}{path}",
            headers=dict(headers),
            json=dict(payload),
        )
        try:
            response = self._client.send(request)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The provider can safely retry these known-unsent requests.
            raise
        except (httpx.TransportError, OSError) as error:
            raise ProviderUnreachableError(
                "openrouter transport is unreachable"
            ) from error
        return response.status_code, response.text

    def stream(
        self, path: str, headers: Mapping[str, str], payload: Mapping[str, object]
    ) -> Generator[str, None, None]:
        """Yield complete SSE data frames; closing the iterator closes the socket."""
        request = self._client.build_request(
            "POST", f"{self._base_url}{path}", headers=dict(headers), json=dict(payload)
        )
        response: httpx.Response | None = None
        try:
            response = self._client.send(request, stream=True)
            with closing(response):
                if not 200 <= response.status_code < 300:
                    response.read()
                    raise ProviderRejectedError(
                        "openrouter rejected the request",
                        status=response.status_code,
                        retry_after=_retry_after(response.headers.get("retry-after")),
                    )
                yield from sse_data(response.iter_bytes())
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if response is None:
                raise
            raise ProviderUnreachableError(
                "openrouter transport is unreachable"
            ) from error
        except httpx.LocalProtocolError as error:
            raise ProviderRejectedError(
                "openrouter request violates the local HTTP protocol"
            ) from error
        except (httpx.TransportError, OSError) as error:
            raise ProviderUnreachableError(
                "openrouter transport is unreachable"
            ) from error


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                return None
            seconds = max(0.0, (timestamp - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class EnvironmentApiKeySource:
    """Read a provider API key from the process environment."""

    def __init__(self, variable: str = "OPENROUTER_API_KEY") -> None:
        self._variable = variable

    def key(self) -> str:
        return os.environ.get(self._variable, "")


__all__ = [
    "EnvironmentApiKeySource",
    "HttpOpenRouterTransport",
]
