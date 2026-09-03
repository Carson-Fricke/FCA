from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from harness.configuration import load_example
from harness.configuration.models import TraderConfig
from harness.core.types import ParquetCodec, QuoteEvent, TradeEvent
from harness.data.alpaca_ws import (
    AlpacaSipInbox,
    AlpacaSipSubscription,
    AlpacaSipWebSocketFeed,
    AlpacaWebSocketProtocolError,
)

ROOT = Path(__file__).parents[3]
START = datetime(2024, 1, 19, 14, 30, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class MockSocket:
    def __init__(
        self,
        frames: list[object],
        *,
        stop: asyncio.Event | None = None,
        stop_after: int | None = None,
        on_receive: Any = None,
    ) -> None:
        self.frames = list(frames)
        self.stop = stop
        self.stop_after = stop_after
        self.on_receive = on_receive
        self.sent: list[Mapping[str, object]] = []
        self.receives = 0
        self.closed = False

    async def send(self, message: str) -> None:
        payload: Any = json.loads(message)
        assert isinstance(payload, dict)
        self.sent.append(payload)

    async def receive(self, timeout_seconds: float) -> str | bytes | None:
        assert timeout_seconds > 0
        self.receives += 1
        if self.on_receive is not None:
            self.on_receive(self.receives)
        value = self.frames.pop(0)
        if self.stop_after == self.receives and self.stop is not None:
            self.stop.set()
        if isinstance(value, BaseException):
            raise value
        if value is None or isinstance(value, bytes):
            return value
        return json.dumps(value, separators=(",", ":"))

    async def close(self) -> None:
        self.closed = True


class MockServer:
    def __init__(self, sockets: list[MockSocket]) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []

    async def connect(self, url: str) -> MockSocket:
        self.urls.append(url)
        return self.sockets.pop(0)


def controls(symbols: list[str]) -> list[object]:
    return [
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "trades": symbols, "quotes": symbols}],
    ]


def quote(timestamp: str = "2024-01-19T14:30:00.123456789Z") -> dict[str, object]:
    return {
        "T": "q",
        "S": "SPY",
        "t": timestamp,
        "bx": "V",
        "bp": 482.1000,
        "bs": 100,
        "ax": "Q",
        "ap": 482.1100,
        "as": 200,
        "z": "B",
    }


def trade(timestamp: str = "2024-01-19T14:30:00.500000Z") -> dict[str, object]:
    return {
        "T": "t",
        "S": "SPY",
        "i": 9001,
        "x": "V",
        "p": 482.105,
        "s": 10,
        "t": timestamp,
    }


def exact(message: dict[str, object]) -> dict[str, object]:
    payload: Any = json.loads(json.dumps(message), parse_float=Decimal)
    assert isinstance(payload, dict)
    return payload


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_deployment_symbols_drive_auth_subscription_and_replay_events() -> None:
    config = load_example("trader", ROOT)
    assert isinstance(config, TraderConfig)
    subscription = AlpacaSipSubscription.from_trader_config(config)
    stop = asyncio.Event()
    socket = MockSocket(
        [*controls(["SPY"]), [quote(), trade()]],
        stop=stop,
        stop_after=4,
    )
    inbox = AlpacaSipInbox()
    feed = AlpacaSipWebSocketFeed.from_trader_config(
        config,
        api_key="key",
        api_secret="secret",
        transport=MockServer([socket]),
        inbox=inbox,
        clock=FakeClock(),
    )

    run(feed.run(stop, max_connections=1))

    assert subscription == AlpacaSipSubscription(("SPY",), ("SPY",))
    assert socket.sent == [
        {"action": "auth", "key": "key", "secret": "secret"},
        {"action": "subscribe", "trades": ["SPY"], "quotes": ["SPY"]},
    ]
    events = inbox.drain()
    assert len(events) == 2
    assert isinstance(events[0], QuoteEvent)
    assert events[0].bid_price == Decimal("482.1")
    assert events[0].ask_quantity == Decimal("200")
    assert events[0].venue == "B"
    assert isinstance(events[1], TradeEvent)
    assert events[1].trade_id == "9001"
    assert events[1].venue == "V"
    assert [event.sequence for event in events] == [0, 1]
    for event in events:
        restored = ParquetCodec.decode(ParquetCodec.encode([event]), type(event))[0]
        assert restored == event
    assert socket.closed


def test_disconnect_reconnects_with_backoff_without_losing_new_events() -> None:
    stop = asyncio.Event()
    first = MockSocket([*controls(["SPY"]), ConnectionError("disconnected")])
    second = MockSocket([*controls(["SPY"]), [trade()]], stop=stop, stop_after=4)
    server = MockServer([first, second])
    clock = FakeClock()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    inbox = AlpacaSipInbox()
    feed = AlpacaSipWebSocketFeed(
        api_key="key",
        api_secret="secret",
        subscription=AlpacaSipSubscription(("SPY",), ("SPY",)),
        transport=server,
        inbox=inbox,
        clock=clock,
        sleep=sleep,
        backoff_seconds=0.25,
    )

    run(feed.run(stop, max_connections=2))

    assert feed.connection_attempts == 2
    assert sleeps == [0.25]
    assert len(server.urls) == 2
    assert first.closed and second.closed
    assert isinstance(inbox.pop(), TradeEvent)


def test_watchdog_reports_a_symbol_that_never_receives_data() -> None:
    stop = asyncio.Event()
    clock = FakeClock()

    def timeout(receive_count: int) -> None:
        if receive_count == 4:
            clock.advance(10)

    socket = MockSocket(
        [*controls(["SPY"]), None],
        stop=stop,
        stop_after=4,
        on_receive=timeout,
    )
    stale: list[tuple[str, ...]] = []
    feed = AlpacaSipWebSocketFeed(
        api_key="key",
        api_secret="secret",
        subscription=AlpacaSipSubscription(("SPY",), ("SPY",)),
        transport=MockServer([socket]),
        inbox=AlpacaSipInbox(),
        clock=clock,
        stale_after=timedelta(seconds=10),
        on_stale=stale.append,
    )

    run(feed.run(stop, max_connections=1))

    assert stale == [("SPY",)]
    assert feed.stale_symbols == ("SPY",)


def test_inbox_timestamp_sanity_is_explicit_and_atomic() -> None:
    inbox = AlpacaSipInbox(
        initial_sequence=7,
        max_future_skew=timedelta(seconds=1),
        max_event_age=timedelta(seconds=10),
    )

    with pytest.raises(AlpacaWebSocketProtocolError, match="future skew"):
        inbox.push(
            [
                exact(trade()),
                exact(quote("2024-01-19T14:30:02.000001Z")),
            ],
            observed_at=START,
        )
    with pytest.raises(AlpacaWebSocketProtocolError, match="maximum event age"):
        inbox.push(
            quote("2024-01-19T14:29:50Z"),
            observed_at=START,
        )

    assert len(inbox) == 0
    assert inbox.next_sequence == 7
    accepted = inbox.push([exact(quote()), exact(trade())], observed_at=START)
    assert [event.sequence for event in accepted] == [7, 8]


def test_inbox_staleness_uses_only_caller_supplied_times() -> None:
    inbox = AlpacaSipInbox()
    active_since = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
    inbox.push(exact(quote()), observed_at=START)

    assert inbox.stale_symbols(
        ["SPY", "AAPL"],
        observed_at=active_since + timedelta(seconds=20),
        active_since=active_since,
        stale_after=timedelta(seconds=15),
    ) == ("AAPL", "SPY")
    assert (
        inbox.stale_symbols(
            ["SPY", "AAPL"],
            observed_at=active_since + timedelta(seconds=10),
            active_since=active_since,
            stale_after=timedelta(seconds=15),
        )
        == ()
    )
