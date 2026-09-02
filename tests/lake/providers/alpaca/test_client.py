import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from lake.configuration import load_settings
from lake.providers.alpaca import (
    AlpacaClient,
    AlpacaHTTPError,
    AlpacaProtocolError,
    NormalizedQuote,
    NormalizedTrade,
)

ROOT = Path(__file__).parents[4]


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200, **headers: str) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers: Mapping[str, str] = headers

    def json(self) -> object:
        return self.payload


class FakeTransport:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _trade(trade_id: int, timestamp: str) -> dict[str, object]:
    return {
        "t": timestamp,
        "x": "V",
        "p": "150.125",
        "s": 10,
        "c": ["@", "I"],
        "i": trade_id,
        "z": "C",
    }


def _quote(timestamp: str) -> dict[str, object]:
    return {
        "t": timestamp,
        "bx": "V",
        "bp": "150.12",
        "bs": 100,
        "ax": "Q",
        "ap": "150.13",
        "as": 200,
        "c": [],
        "z": "C",
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_trades_follow_cursor_pages_and_retry_after_without_duplicates() -> None:
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "trades": {"AAPL": [_trade(1, "2024-01-19T14:30:00Z")]},
                    "next_page_token": "p2",
                }
            ),
            FakeResponse({}, status_code=429, **{"Retry-After": "0"}),
            FakeResponse(
                {
                    "trades": {"AAPL": [_trade(2, "2024-01-19T14:31:00Z")]},
                    "next_page_token": None,
                }
            ),
        ]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def exercise() -> tuple[NormalizedTrade, ...]:
        async with AlpacaClient(
            transport=transport,
            max_attempts=3,
            sleep=sleep,
        ) as client:
            return await client.fetch_trades(
                "aapl", date(2024, 1, 19), date(2024, 1, 20), limit=100
            )

    trades = _run(exercise())

    assert [trade.trade_id for trade in trades] == [1, 2]
    assert all(isinstance(trade, NormalizedTrade) for trade in trades)
    assert len(transport.calls) == 3
    assert transport.calls[0]["params"] == {
        "start": "2024-01-19T00:00:00Z",
        "end": "2024-01-20T00:00:00Z",
        "feed": "sip",
        "adjustment": "raw",
        "limit": 100,
        "sort": "asc",
    }
    assert transport.calls[1]["params"]["page_token"] == "p2"
    assert transport.calls[2]["params"]["page_token"] == "p2"
    assert sleeps == [0.0]


def test_quotes_are_normalized_to_the_t021_field_contract() -> None:
    transport = FakeTransport(
        [FakeResponse({"quotes": {"MSFT": [_quote("2024-01-19T14:30:00.123456Z")]}})]
    )

    async def exercise() -> tuple[NormalizedQuote, ...]:
        async with AlpacaClient(transport=transport) as client:
            return await client.fetch_quotes(
                [" msft ", "MSFT"],
                datetime(2024, 1, 19, 14, 30, tzinfo=UTC),
                datetime(2024, 1, 19, 15, 30, tzinfo=UTC),
            )

    quotes = _run(exercise())

    assert quotes == (
        NormalizedQuote(
            timestamp=datetime(2024, 1, 19, 14, 30, 0, 123456, tzinfo=UTC),
            symbol="MSFT",
            bid_exchange="V",
            bid_price=Decimal("150.12"),
            bid_size=100,
            ask_exchange="Q",
            ask_price=Decimal("150.13"),
            ask_size=200,
            conditions=(),
            tape="C",
        ),
    )
    assert len(transport.calls) == 1


def test_multiple_symbols_are_bounded_and_settings_are_applied() -> None:
    class ConcurrentTransport:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.calls: list[dict[str, object]] = []

        async def request(
            self,
            method: str,
            url: str,
            *,
            params: Mapping[str, str | int],
            headers: Mapping[str, str],
            timeout: float,
        ) -> FakeResponse:
            del method, params, headers
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append({"url": url, "timeout": timeout})
            await asyncio.sleep(0)
            self.active -= 1
            symbol = url.split("/stocks/")[1].split("/")[0]
            return FakeResponse(
                {"trades": {symbol: [_trade(1, "2024-01-19T14:30:00Z")]}}
            )

    transport = ConcurrentTransport()
    settings = load_settings(
        ROOT,
        environ={
            "LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY": "2",
            "LAKE_PROVIDER__REQUEST__TIMEOUT_SECONDS": "7",
            "LAKE_PROVIDER__REQUEST__RETRY__MAX_ATTEMPTS": "3",
        },
    )

    async def exercise() -> tuple[NormalizedTrade, ...]:
        async with AlpacaClient.from_settings(
            settings.provider, transport=transport
        ) as client:
            return await client.fetch_trades(
                ["AAPL", "MSFT", "NVDA"],
                "2024-01-19T00:00:00Z",
                "2024-01-20T00:00:00Z",
            )

    trades = _run(exercise())

    assert transport.maximum == 2
    assert [trade.symbol for trade in trades] == ["AAPL", "MSFT", "NVDA"]
    assert all(call["timeout"] == 7.0 for call in transport.calls)


def test_transient_transport_errors_retry_but_cancellation_propagates() -> None:
    transport = FakeTransport(
        [httpx.ReadTimeout("temporary"), FakeResponse({"trades": {"AAPL": []}})]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def exercise() -> tuple[NormalizedTrade, ...]:
        async with AlpacaClient(
            transport=transport,
            max_attempts=2,
            backoff_seconds=0.125,
            sleep=sleep,
        ) as client:
            return await client.fetch_trades("AAPL", "start", "end")

    assert _run(exercise()) == ()
    assert sleeps == [0.125]

    class BlockingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__([])
            self.started = asyncio.Event()

        async def request(
            self,
            method: str,
            url: str,
            *,
            params: Mapping[str, str | int],
            headers: Mapping[str, str],
            timeout: float,
        ) -> FakeResponse:
            del method, url, params, headers, timeout
            self.started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    async def cancel_exercise() -> None:
        blocking = BlockingTransport()
        client = AlpacaClient(transport=blocking)
        task = asyncio.create_task(client.fetch_trades("AAPL", "start", "end"))
        await blocking.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()

    _run(cancel_exercise())


def test_malformed_cursor_and_exhausted_http_errors_are_explicit() -> None:
    repeated = FakeTransport(
        [
            FakeResponse(
                {
                    "trades": {"AAPL": [_trade(1, "2024-01-19T14:30:00Z")]},
                    "next_page_token": "same",
                }
            ),
            FakeResponse({"trades": {"AAPL": []}, "next_page_token": "same"}),
        ]
    )

    async def repeated_cursor() -> None:
        async with AlpacaClient(transport=repeated) as client:
            await client.fetch_trades("AAPL", "start", "end")

    with pytest.raises(AlpacaProtocolError, match="repeated"):
        _run(repeated_cursor())

    limited = FakeTransport([FakeResponse({}, status_code=429, **{"Retry-After": "0"})])

    async def exhausted() -> None:
        async with AlpacaClient(transport=limited, max_attempts=1) as client:
            await client.fetch_quotes("AAPL", "start", "end")

    with pytest.raises(AlpacaHTTPError) as error:
        _run(exhausted())
    assert error.value.status_code == 429

    missing_rows = FakeTransport([FakeResponse({})])

    async def missing_rows_response() -> None:
        async with AlpacaClient(transport=missing_rows) as client:
            await client.fetch_trades("AAPL", "start", "end")

    with pytest.raises(AlpacaProtocolError, match="missing 'trades'"):
        _run(missing_rows_response())
