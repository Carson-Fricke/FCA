import asyncio
import email.utils
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
import pytest

from lake.reference.http import (
    DEFAULT_REQUESTS_PER_SECOND,
    RateLimiter,
    ReferenceHTTPClient,
    ReferenceHTTPError,
)

SEC_USER_AGENT: Final = "FCA market lake ingest AdminContact@example.com"
TARGET_URL: Final = "https://data.sec.gov/api/xbrl/companyfacts/CIK0003200193.json"


class FakeFloatClock:
    def __init__(self) -> None:
        self.moment = 0.0

    def __call__(self) -> float:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += seconds


class FakeSleep:
    def __init__(self, clock: FakeFloatClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)


class FakeDatetimeClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers: Mapping[str, str] = dict(headers or {})


class FakeTransport:
    def __init__(self, responses: Sequence[FakeResponse | Exception]) -> None:
        self.responses: list[FakeResponse | Exception] = list(responses)
        self.requests: list[tuple[str, str, Mapping[str, str], float]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.requests.append((method, url, dict(headers), timeout))
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_default_rate_is_the_sec_fair_access_cap() -> None:
    assert DEFAULT_REQUESTS_PER_SECOND == 10.0


def test_rate_limiter_paces_requests_at_one_per_min_interval() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(requests_per_second=10.0, clock=clock, sleep=sleep)

    async def scenario() -> None:
        for _ in range(4):
            await limiter.acquire()

    asyncio.run(scenario())

    assert len(sleep.waits) == 3
    assert all(wait == pytest.approx(0.1) for wait in sleep.waits)


def test_rate_limiter_serializes_concurrent_callers() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(requests_per_second=10.0, clock=clock, sleep=sleep)

    async def scenario() -> None:
        await asyncio.gather(*(limiter.acquire() for _ in range(10)))

    asyncio.run(scenario())

    assert len(sleep.waits) == 9
    assert all(wait == pytest.approx(0.1) for wait in sleep.waits)
    assert clock.moment == pytest.approx(0.9)


def test_rate_limiter_rejects_non_positive_rates() -> None:
    for rate in (0.0, -1.0):
        with pytest.raises(ValueError, match="requests_per_second must be positive"):
            RateLimiter(requests_per_second=rate)


def test_client_sends_descriptive_user_agent_and_timeout() -> None:
    transport = FakeTransport([FakeResponse(200, content=b"payload")])

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            timeout_seconds=12.5,
            rate_limiter=RateLimiter(
                requests_per_second=10.0,
                clock=FakeFloatClock(),
                sleep=FakeSleep(FakeFloatClock()),
            ),
        )
        try:
            payload = await client.get_bytes(TARGET_URL)
        finally:
            await client.aclose()
        assert payload == b"payload"

    asyncio.run(scenario())

    assert transport.requests == [
        (
            "GET",
            TARGET_URL,
            {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            12.5,
        )
    ]


def test_client_retries_retryable_status_codes_with_backoff() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    transport = FakeTransport(
        [
            FakeResponse(500),
            FakeResponse(503),
            FakeResponse(200, content=b"ok"),
        ]
    )

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            rate_limiter=RateLimiter(
                requests_per_second=10.0, clock=clock, sleep=sleep
            ),
            sleep=sleep,
        )
        try:
            assert await client.get_bytes(TARGET_URL) == b"ok"
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert sleep.waits == [0.5, 1.0]


def test_client_raises_after_the_retry_budget_is_exhausted() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    transport = FakeTransport([FakeResponse(500)] * 5)

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            rate_limiter=RateLimiter(
                requests_per_second=10.0, clock=clock, sleep=sleep
            ),
            sleep=sleep,
        )
        try:
            with pytest.raises(ReferenceHTTPError, match="HTTP 500"):
                await client.get_bytes(TARGET_URL)
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert len(transport.requests) == 5
    assert sleep.waits == [0.5, 1.0, 2.0, 4.0]


def test_client_fails_fast_on_non_retryable_status_codes() -> None:
    transport = FakeTransport([FakeResponse(404)])

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            rate_limiter=RateLimiter(
                requests_per_second=10.0,
                clock=FakeFloatClock(),
                sleep=FakeSleep(FakeFloatClock()),
            ),
        )
        try:
            with pytest.raises(ReferenceHTTPError, match="HTTP 404"):
                await client.get_bytes(TARGET_URL)
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert len(transport.requests) == 1


def test_client_honors_numeric_retry_after_on_rate_limit_responses() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    transport = FakeTransport(
        [
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, content=b"ok"),
        ]
    )

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            rate_limiter=RateLimiter(
                requests_per_second=10.0, clock=clock, sleep=sleep
            ),
            sleep=sleep,
            clock=FakeDatetimeClock(datetime(2024, 1, 1, tzinfo=UTC)),
        )
        try:
            assert await client.get_bytes(TARGET_URL) == b"ok"
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert sleep.waits == [7.0]


def test_client_honors_http_date_retry_after_values() -> None:
    retry_at = datetime(2024, 1, 1, 0, 0, 10, tzinfo=UTC)
    limiter_clock = FakeFloatClock()
    limiter_sleep = FakeSleep(limiter_clock)
    sleep = FakeSleep(FakeFloatClock())
    transport = FakeTransport(
        [
            FakeResponse(
                429,
                headers={"Retry-After": email.utils.format_datetime(retry_at)},
            ),
            FakeResponse(200, content=b"ok"),
        ]
    )

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            rate_limiter=RateLimiter(
                requests_per_second=10.0, clock=limiter_clock, sleep=limiter_sleep
            ),
            sleep=sleep,
            clock=FakeDatetimeClock(retry_at - timedelta(seconds=3)),
        )
        try:
            assert await client.get_bytes(TARGET_URL) == b"ok"
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert sleep.waits == [3.0]


def test_client_falls_back_to_backoff_on_unparsable_retry_after() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    transport = FakeTransport(
        [
            FakeResponse(429, headers={"Retry-After": "garbage"}),
            FakeResponse(200, content=b"ok"),
        ]
    )

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            rate_limiter=RateLimiter(
                requests_per_second=10.0, clock=clock, sleep=sleep
            ),
            sleep=sleep,
            clock=FakeDatetimeClock(datetime(2024, 1, 1, tzinfo=UTC)),
        )
        try:
            await client.get_bytes(TARGET_URL)
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert sleep.waits == [0.5]


def test_client_retries_transport_errors_then_raises() -> None:
    clock = FakeFloatClock()
    sleep = FakeSleep(clock)
    transport = FakeTransport(
        [
            httpx.ConnectError("boom"),
            httpx.ReadTimeout("slow"),
            httpx.ConnectError("boom"),
        ]
    )

    async def scenario() -> None:
        client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=transport,
            max_attempts=3,
            rate_limiter=RateLimiter(
                requests_per_second=10.0, clock=clock, sleep=sleep
            ),
            sleep=sleep,
        )
        try:
            with pytest.raises(httpx.ConnectError):
                await client.get_bytes(TARGET_URL)
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert len(transport.requests) == 3
    assert sleep.waits == [0.5, 1.0]


def test_client_decodes_json_and_rejects_malformed_payloads() -> None:
    good = FakeTransport([FakeResponse(200, content=b'{"cik": 320193}')])
    bad = FakeTransport([FakeResponse(200, content=b"not json")])

    async def scenario() -> None:
        good_client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=good,
            rate_limiter=RateLimiter(
                requests_per_second=10.0,
                clock=FakeFloatClock(),
                sleep=FakeSleep(FakeFloatClock()),
            ),
            sleep=FakeSleep(FakeFloatClock()),
        )
        try:
            assert await good_client.get_json(TARGET_URL) == {"cik": 320193}
        finally:
            await good_client.aclose()

        bad_client = ReferenceHTTPClient(
            user_agent=SEC_USER_AGENT,
            transport=bad,
            rate_limiter=RateLimiter(
                requests_per_second=10.0,
                clock=FakeFloatClock(),
                sleep=FakeSleep(FakeFloatClock()),
            ),
            sleep=FakeSleep(FakeFloatClock()),
        )
        try:
            with pytest.raises(ReferenceHTTPError, match=TARGET_URL):
                await bad_client.get_json(TARGET_URL)
        finally:
            await bad_client.aclose()

    asyncio.run(scenario())


def test_client_validates_the_sec_user_agent_format() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        ReferenceHTTPClient(user_agent="anonymous")
    with pytest.raises(ValueError, match="user_agent"):
        ReferenceHTTPClient(user_agent="   ")
