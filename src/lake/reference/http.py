"""Fair-access HTTP client for reference data providers.

SEC EDGAR requires a descriptive ``User-Agent`` header and caps programmatic
access at 10 requests per second; Nasdaq Trader serves the symbol directory
over plain HTTPS. The client owns a rate limiter applied to every request
attempt and mirrors the Alpaca client retry discipline: exponential backoff
with ``Retry-After`` honored for rate-limit responses. Transport and sleeping
are injectable so tests never touch the network.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, Protocol, cast

import httpx

type Sleep = Callable[[float], Awaitable[None]]
type FloatClock = Callable[[], float]
type DatetimeClock = Callable[[], datetime]

DEFAULT_REQUESTS_PER_SECOND: Final = 10.0


class ReferenceHTTPError(RuntimeError):
    """A non-successful provider response after the retry policy is exhausted."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"reference request failed with HTTP {status_code}: {url}")


class RateLimiter:
    """Pace concurrent request attempts at a fixed requests-per-second rate."""

    def __init__(
        self,
        *,
        requests_per_second: float,
        clock: FloatClock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.requests_per_second = float(requests_per_second)
        self._min_interval = 1.0 / self.requests_per_second
        self._clock = time.monotonic if clock is None else clock
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._next_available = float("-inf")
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until the next request slot, reserving it for this caller."""
        async with self._lock:
            now = self._clock()
            wait = self._next_available - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_available = max(now, self._next_available) + self._min_interval


class ReferenceResponse(Protocol):
    """Response surface required by the client and test fakes."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes


class ReferenceTransport(Protocol):
    """Request surface implemented by ``httpx.AsyncClient`` and test fakes."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> ReferenceResponse:
        """Send one asynchronous HTTP request."""


class ReferenceFetchClient(Protocol):
    """Fetch surface of ``ReferenceHTTPClient`` consumed by ingest pipelines.

    Ingest entrypoints only need ``get_bytes`` and ``get_json``, so offline
    test doubles can stand in for the rate-limited client.
    """

    async def get_bytes(self, url: str) -> bytes:
        """Return the raw payload bytes for one URL."""

    async def get_json(self, url: str) -> object:
        """Return the decoded JSON document for one URL."""


class ReferenceHTTPClient:
    """Fetch reference payloads with fair-access pacing and bounded retries.

    ``transport`` is optional in application code and can be an
    ``httpx.AsyncClient``. A fake implementing :class:`ReferenceTransport` can
    be supplied for tests. The client only owns and closes the default client
    it creates itself.
    """

    RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset(
        {408, 425, 429, 500, 502, 503, 504}
    )

    def __init__(
        self,
        *,
        user_agent: str,
        rate_limiter: RateLimiter | None = None,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        honor_retry_after: bool = True,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        transport: ReferenceTransport | None = None,
        sleep: Sleep | None = None,
        clock: DatetimeClock | None = None,
    ) -> None:
        _validate_user_agent(user_agent)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        if max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds cannot be negative")

        self.user_agent = user_agent
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max_attempts
        self.honor_retry_after = honor_retry_after
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._rate_limiter = (
            RateLimiter(requests_per_second=DEFAULT_REQUESTS_PER_SECOND)
            if rate_limiter is None
            else rate_limiter
        )
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._clock = _utc_now if clock is None else clock
        self._default_client: httpx.AsyncClient | None = None

        if transport is None:
            self._default_client = httpx.AsyncClient(timeout=self.timeout_seconds)
            self._transport: ReferenceTransport = cast(
                ReferenceTransport, self._default_client
            )
        else:
            self._transport = transport
        self._headers: dict[str, str] = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        }

    async def __aenter__(self) -> ReferenceHTTPClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-created HTTP client, if there is one."""
        if self._default_client is not None:
            client = self._default_client
            self._default_client = None
            await client.aclose()

    async def get_bytes(self, url: str) -> bytes:
        """Fetch one payload with pacing, retries, and the User-Agent header."""
        response = await self._request(url)
        return response.content

    async def get_json(self, url: str) -> object:
        """Fetch and decode one JSON payload."""
        response = await self._request(url)
        try:
            return json.loads(response.content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ReferenceHTTPError(200, url) from error

    async def _request(self, url: str) -> ReferenceResponse:
        for attempt in range(1, self.max_attempts + 1):
            await self._rate_limiter.acquire()
            try:
                response = await self._transport.request(
                    "GET",
                    url,
                    headers=self._headers,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TransportError, TimeoutError):
                if attempt == self.max_attempts:
                    raise
                await self._sleep(self._backoff(attempt))
                continue

            if 200 <= response.status_code < 300:
                return response
            if response.status_code not in self.RETRYABLE_STATUS_CODES:
                raise ReferenceHTTPError(response.status_code, url)
            if attempt == self.max_attempts:
                raise ReferenceHTTPError(response.status_code, url)

            delay = self._backoff(attempt)
            if response.status_code == 429 and self.honor_retry_after:
                retry_after = _retry_after_seconds(response.headers, self._clock())
                if retry_after is not None:
                    delay = min(retry_after, self.max_backoff_seconds)
            await self._sleep(delay)

        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> float:
        return min(
            self.backoff_seconds * (2 ** (attempt - 1)), self.max_backoff_seconds
        )


def _validate_user_agent(user_agent: str) -> None:
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ValueError("user_agent must be a non-empty descriptive string")
    if "@" not in user_agent:
        raise ValueError(
            "user_agent must follow the SEC format "
            "'Sample Company Name AdminContact@domain.com'"
        )


def _retry_after_seconds(headers: Mapping[str, str], now: datetime) -> float | None:
    value: str | None = None
    for key, header_value in headers.items():
        if key.lower() == "retry-after":
            value = header_value
            break
    if value is None:
        return None

    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DEFAULT_REQUESTS_PER_SECOND",
    "RateLimiter",
    "ReferenceFetchClient",
    "ReferenceHTTPClient",
    "ReferenceHTTPError",
    "ReferenceResponse",
    "ReferenceTransport",
]
