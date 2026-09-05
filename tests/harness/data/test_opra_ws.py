"""Focused tests for the live Alpaca OPRA WebSocket feed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from harness.core.symbols import OsiSymbol
from harness.core.types import ParquetCodec, QuoteEvent, TradeEvent
from harness.data.alpaca_ws import AlpacaWebSocketProtocolError
from harness.data.opra_ws import (
    AlpacaOpraInbox,
    AlpacaOpraSubscription,
    AlpacaOpraWebSocketFeed,
    OpraRotationDelta,
    rotation_delta,
)
from harness.data.options_universe import OptionUniverseSnapshot, SelectionMode
from lake.catalog.models import ContractRecord, OptionType

OPRA_URL = "wss://stream.data.alpaca.markets/v1beta1/opra"
START = datetime(2024, 1, 19, 14, 30, 1, tzinfo=UTC)
JAN = date(2024, 1, 19)
FEB = date(2024, 2, 16)
C150 = "AAPL240119C00150000"
P150 = "AAPL240119P00150000"
FEB_C150 = "AAPL240216C00150000"


class FakeClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class MockSocket:
    """In-memory OPRA socket whose receive blocks until frames arrive."""

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
        self.subscribed = asyncio.Event()
        self.unsubscribed = asyncio.Event()
        self._frames_available = asyncio.Event()

    async def send(self, message: str) -> None:
        payload: Any = json.loads(message)
        assert isinstance(payload, dict)
        self.sent.append(payload)
        if payload.get("action") == "subscribe":
            self.subscribed.set()
        if payload.get("action") == "unsubscribe":
            self.unsubscribed.set()

    async def receive(self, timeout_seconds: float) -> str | bytes | None:
        assert timeout_seconds > 0
        while not self.frames:
            if self.closed:
                raise ConnectionError("mock socket closed")
            if self.stop is not None and self.stop.is_set():
                return None
            self._frames_available.clear()
            await self._frames_available.wait()
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

    def enqueue(self, frame: object) -> None:
        self.frames.append(frame)
        self._frames_available.set()

    async def close(self) -> None:
        self.closed = True
        self._frames_available.set()


class MockServer:
    def __init__(self, sockets: list[MockSocket]) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []

    async def connect(self, url: str) -> MockSocket:
        self.urls.append(url)
        return self.sockets.pop(0)


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def controls(symbols: list[str]) -> list[object]:
    return [
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "trades": symbols, "quotes": symbols}],
    ]


def opra_quote(
    timestamp: str = "2024-01-19T14:30:00.123456789Z", symbol: str = C150
) -> dict[str, object]:
    return {
        "T": "q",
        "S": symbol,
        "x": "CBOE",
        "c": "A",
        "bp": 4.85,
        "bs": 76,
        "ap": 5.05,
        "as": 43,
        "t": timestamp,
    }


def opra_trade(
    timestamp: str = "2024-01-19T14:30:00.500000Z", symbol: str = C150
) -> dict[str, object]:
    return {
        "T": "t",
        "S": symbol,
        "i": 6183,
        "x": "CBOE",
        "p": 4.95,
        "s": 1,
        "c": "regular",
        "t": timestamp,
    }


def exact(message: dict[str, object]) -> dict[str, object]:
    payload: Any = json.loads(json.dumps(message), parse_float=Decimal)
    assert isinstance(payload, dict)
    return payload


def contract(
    symbol: str,
    *,
    expiry: date = JAN,
    strike: str = "150",
    right: OptionType = OptionType.CALL,
    listed: date | None = None,
) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying="AAPL",
        expiration_date=expiry,
        strike_price=Decimal(strike),
        option_type=right,
        listed_date=listed,
    )


JAN_CALL = contract(C150)
JAN_PUT = contract(P150, right=OptionType.PUT)
FEB_CALL = contract(FEB_C150, expiry=FEB)


def snapshot(
    contracts: tuple[ContractRecord, ...], *, as_of: date = JAN
) -> OptionUniverseSnapshot:
    return OptionUniverseSnapshot(
        underlying="AAPL",
        as_of=as_of,
        mode=SelectionMode.STATIC_OSI,
        contracts=contracts,
    )


def test_snapshot_contracts_drive_auth_subscription_and_replay_events() -> None:
    stop = asyncio.Event()
    socket = MockSocket(
        [*controls([C150, P150]), [opra_quote(), opra_trade(symbol=P150)]],
        stop=stop,
        stop_after=4,
    )
    server = MockServer([socket])
    inbox = AlpacaOpraInbox()
    feed = AlpacaOpraWebSocketFeed.from_universe(
        snapshot((JAN_CALL, JAN_PUT)),
        api_key="key",
        api_secret="secret",
        transport=server,
        inbox=inbox,
        clock=FakeClock(),
    )

    run(feed.run(stop, max_connections=1))

    assert server.urls == [OPRA_URL]
    assert socket.sent == [
        {"action": "auth", "key": "key", "secret": "secret"},
        {"action": "subscribe", "trades": [C150, P150], "quotes": [C150, P150]},
    ]
    events = inbox.drain()
    assert [event.sequence for event in events] == [0, 1]
    quote = events[0]
    assert isinstance(quote, QuoteEvent)
    assert quote.symbol.value == C150
    assert isinstance(quote.symbol, OsiSymbol)
    assert str(quote.bid_price) == "4.85"
    assert str(quote.bid_quantity) == "76"
    assert str(quote.ask_price) == "5.05"
    assert str(quote.ask_quantity) == "43"
    assert quote.venue == "CBOE"
    assert quote.event_id == (
        f"alpaca-opra:q:{C150}:2024-01-19T14:30:00.123456+00:00:0"
    )
    trade = events[1]
    assert isinstance(trade, TradeEvent)
    assert trade.symbol.value == P150
    assert str(trade.price) == "4.95"
    assert str(trade.quantity) == "1"
    assert trade.trade_id == "6183"
    assert trade.venue == "CBOE"
    assert trade.event_id.startswith(f"alpaca-opra:t:{P150}:")
    for event in events:
        restored = ParquetCodec.decode(ParquetCodec.encode([event]), type(event))[0]
        assert restored == event
    assert socket.closed


def test_rotation_swaps_live_contract_sets_without_reconnecting() -> None:
    async def scenario() -> tuple[
        AlpacaOpraWebSocketFeed, AlpacaOpraInbox, MockSocket, MockServer
    ]:
        stop = asyncio.Event()
        clock = FakeClock()
        streaming = asyncio.Event()
        socket = MockSocket(
            [*controls([C150, P150]), None],
            stop=stop,
            stop_after=6,
            on_receive=lambda count: streaming.set() if count == 4 else None,
        )
        server = MockServer([socket])
        inbox = AlpacaOpraInbox()
        feed = AlpacaOpraWebSocketFeed(
            api_key="key",
            api_secret="secret",
            subscription=AlpacaOpraSubscription((C150, P150), (C150, P150)),
            transport=server,
            inbox=inbox,
            clock=clock,
        )
        run_task = asyncio.create_task(feed.run(stop))
        await socket.subscribed.wait()
        await streaming.wait()
        delta = await feed.rotate(AlpacaOpraSubscription((FEB_C150,), (FEB_C150,)))
        assert delta == OpraRotationDelta(
            unsubscribe_trades=(C150, P150),
            unsubscribe_quotes=(C150, P150),
            subscribe_trades=(FEB_C150,),
            subscribe_quotes=(FEB_C150,),
        )
        assert socket.sent[-2:] == [
            {
                "action": "unsubscribe",
                "trades": [C150, P150],
                "quotes": [C150, P150],
            },
            {"action": "subscribe", "trades": [FEB_C150], "quotes": [FEB_C150]},
        ]
        socket.enqueue(
            [{"T": "subscription", "trades": [FEB_C150], "quotes": [FEB_C150]}]
        )
        socket.enqueue([opra_trade("2024-01-19T14:30:02.500000Z", symbol=FEB_C150)])
        await run_task
        return feed, inbox, socket, server

    feed, inbox, socket, server = run(scenario())

    assert feed.connection_attempts == 1
    assert len(server.urls) == 1
    assert feed.stale_symbols == ()
    events = inbox.drain()
    assert len(events) == 1
    assert isinstance(events[0], TradeEvent)
    assert events[0].symbol.value == FEB_C150
    assert [event.sequence for event in events] == [0]
    assert socket.closed


def test_rotation_to_snapshot_uses_only_point_in_time_contracts() -> None:
    async def scenario() -> tuple[OpraRotationDelta, MockSocket, int]:
        stop = asyncio.Event()
        streaming = asyncio.Event()
        socket = MockSocket(
            [*controls([C150, P150]), None],
            stop=stop,
            stop_after=5,
            on_receive=lambda count: streaming.set() if count == 4 else None,
        )
        inbox = AlpacaOpraInbox()
        feed = AlpacaOpraWebSocketFeed(
            api_key="key",
            api_secret="secret",
            subscription=AlpacaOpraSubscription((C150, P150), (C150, P150)),
            transport=MockServer([socket]),
            inbox=inbox,
            clock=FakeClock(),
        )
        run_task = asyncio.create_task(feed.run(stop))
        await socket.subscribed.wait()
        await streaming.wait()
        rolled = snapshot((JAN_CALL, FEB_CALL), as_of=FEB)
        delta = await feed.rotate_to_snapshot(rolled)
        socket.enqueue([opra_quote(symbol=FEB_C150)])
        await run_task
        return delta, socket, len(inbox.drain())

    delta, socket, event_count = run(scenario())

    assert delta == OpraRotationDelta(
        unsubscribe_trades=(C150, P150),
        unsubscribe_quotes=(C150, P150),
        subscribe_trades=(FEB_C150,),
        subscribe_quotes=(FEB_C150,),
    )
    assert socket.sent[-1] == {
        "action": "subscribe",
        "trades": [FEB_C150],
        "quotes": [FEB_C150],
    }
    assert event_count == 1


def test_rotation_requires_a_live_connection_and_opra_subscription() -> None:
    feed = AlpacaOpraWebSocketFeed(
        api_key="key",
        api_secret="secret",
        subscription=AlpacaOpraSubscription((C150,), (C150,)),
        transport=MockServer([]),
        inbox=AlpacaOpraInbox(),
        clock=FakeClock(),
    )

    with pytest.raises(AlpacaWebSocketProtocolError, match="before connecting"):
        run(feed.rotate(AlpacaOpraSubscription((FEB_C150,), (FEB_C150,))))
    with pytest.raises(TypeError, match="AlpacaOpraSubscription"):
        run(feed.rotate(cast("AlpacaOpraSubscription", object())))


def test_disconnect_reconnects_with_backoff_and_keeps_option_events() -> None:
    stop = asyncio.Event()
    first = MockSocket([*controls([C150]), ConnectionError("disconnected")])
    second = MockSocket([*controls([C150]), [opra_trade()]], stop=stop, stop_after=4)
    server = MockServer([first, second])
    clock = FakeClock()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    inbox = AlpacaOpraInbox()
    feed = AlpacaOpraWebSocketFeed(
        api_key="key",
        api_secret="secret",
        subscription=AlpacaOpraSubscription((C150,), (C150,)),
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
    event = inbox.pop()
    assert isinstance(event, TradeEvent)
    assert event.symbol.value == C150


def test_watchdog_reports_option_contracts_that_never_receive_data() -> None:
    stop = asyncio.Event()
    clock = FakeClock()

    def timeout(receive_count: int) -> None:
        if receive_count == 4:
            clock.advance(10)

    socket = MockSocket(
        [*controls([C150]), None],
        stop=stop,
        stop_after=4,
        on_receive=timeout,
    )
    stale: list[tuple[str, ...]] = []
    feed = AlpacaOpraWebSocketFeed(
        api_key="key",
        api_secret="secret",
        subscription=AlpacaOpraSubscription((C150,), (C150,)),
        transport=MockServer([socket]),
        inbox=AlpacaOpraInbox(),
        clock=clock,
        stale_after=timedelta(seconds=10),
        on_stale=stale.append,
    )

    run(feed.run(stop, max_connections=1))

    assert stale == [(C150,)]
    assert feed.stale_symbols == (C150,)


def test_subscriptions_validate_osi_symbols_and_expose_underlying_joins() -> None:
    with pytest.raises(ValueError, match="invalid OSI symbol"):
        AlpacaOpraSubscription(("SPY",), ())
    with pytest.raises(ValueError, match="invalid OSI symbol"):
        AlpacaOpraSubscription(("AAPL240119C0015000",), ())
    with pytest.raises(ValueError, match="duplicate OSI symbols"):
        AlpacaOpraSubscription((C150, C150), ())
    with pytest.raises(ValueError, match="at least one OPRA"):
        AlpacaOpraSubscription((), ())
    with pytest.raises(TypeError, match="sequence"):
        AlpacaOpraSubscription(cast("tuple[str, ...]", C150), ())

    subscription = AlpacaOpraSubscription((P150, C150), (C150,))
    assert subscription.trades == (C150, P150)
    assert subscription.quotes == (C150,)
    assert subscription.symbols == (C150, P150)
    assert subscription.underlyings == ("AAPL",)
    mixed = AlpacaOpraSubscription(("SPY240119C00500000", C150), ())
    assert mixed.underlyings == ("AAPL", "SPY")


def test_snapshot_membership_excludes_expired_and_future_contracts() -> None:
    expired = snapshot((JAN_CALL, FEB_CALL), as_of=FEB)
    subscription = AlpacaOpraSubscription.from_snapshot(expired)
    assert subscription.trades == (FEB_C150,)
    assert subscription.quotes == (FEB_C150,)

    future_listed = contract(
        "AAPL240319C00150000", expiry=date(2024, 3, 19), listed=date(2024, 3, 1)
    )
    clipped = snapshot((JAN_CALL, future_listed))
    subscription = AlpacaOpraSubscription.from_snapshot(clipped)
    assert subscription.trades == (C150,)

    quotes_only = AlpacaOpraSubscription.from_snapshot(clipped, include_trades=False)
    assert quotes_only.trades == ()
    assert quotes_only.quotes == (C150,)

    with pytest.raises(ValueError, match="at least one OPRA"):
        AlpacaOpraSubscription.from_snapshot(snapshot(()))
    with pytest.raises(TypeError, match="OptionUniverseSnapshot"):
        AlpacaOpraSubscription.from_snapshot(cast("OptionUniverseSnapshot", object()))


def test_rotation_delta_separates_removed_and_added_channels() -> None:
    current = AlpacaOpraSubscription((C150, P150), (C150,))
    target = AlpacaOpraSubscription((C150, FEB_C150), (C150, FEB_C150))

    delta = rotation_delta(current, target)

    assert delta == OpraRotationDelta(
        unsubscribe_trades=(P150,),
        unsubscribe_quotes=(),
        subscribe_trades=(FEB_C150,),
        subscribe_quotes=(FEB_C150,),
    )
    assert rotation_delta(target, target).is_empty
    assert not rotation_delta(current, target).is_empty

    with pytest.raises(TypeError, match="AlpacaOpraSubscription"):
        rotation_delta(cast("AlpacaOpraSubscription", object()), target)


def test_inbox_normalizes_opra_frames_and_rejects_non_osi_symbols() -> None:
    inbox = AlpacaOpraInbox()

    message = exact(opra_quote())
    message["S"] = "SPY"
    with pytest.raises(AlpacaWebSocketProtocolError, match="OSI option symbol"):
        inbox.push(message, observed_at=START)
    assert len(inbox) == 0
    assert inbox.next_sequence == 0

    trade_message = exact(opra_trade())
    trade_message["S"] = "BTC/USD"
    with pytest.raises(AlpacaWebSocketProtocolError, match="OSI option symbol"):
        inbox.push(trade_message, observed_at=START)
    assert len(inbox) == 0

    events = inbox.push(
        [exact(opra_quote()), exact(opra_trade(symbol=P150))], observed_at=START
    )
    assert [event.sequence for event in events] == [0, 1]
    assert isinstance(events[0], QuoteEvent)
    assert events[0].venue == "CBOE"
    assert isinstance(events[1], TradeEvent)
    assert events[1].venue == "CBOE"
    assert {str(event.symbol) for event in events} == {C150, P150}


def test_inbox_timestamp_sanity_is_explicit_and_atomic() -> None:
    inbox = AlpacaOpraInbox(
        initial_sequence=7,
        max_future_skew=timedelta(seconds=1),
        max_event_age=timedelta(seconds=10),
    )

    with pytest.raises(AlpacaWebSocketProtocolError, match="future skew"):
        inbox.push(
            [
                exact(opra_trade()),
                exact(opra_trade("2024-01-19T14:30:02.000001Z")),
            ],
            observed_at=START,
        )
    with pytest.raises(AlpacaWebSocketProtocolError, match="maximum event age"):
        inbox.push(opra_quote("2024-01-19T14:29:50Z"), observed_at=START)

    assert len(inbox) == 0
    assert inbox.next_sequence == 7
    accepted = inbox.push(
        [exact(opra_quote()), exact(opra_trade(symbol=P150))], observed_at=START
    )
    assert [event.sequence for event in accepted] == [7, 8]


def test_trader_config_construction_is_refused_in_favor_of_universes() -> None:
    with pytest.raises(NotImplementedError, match="from_universe"):
        AlpacaOpraWebSocketFeed.from_trader_config()
