from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

from harness.configuration import load_example
from harness.configuration.models import (
    DeploymentConfig,
    FeeConfig,
    FeeRuleConfig,
    ResourceLimitsConfig,
    RiskPipelineConfig,
    RiskRuleConfig,
    RuntimeAdapterConfig,
    StrategyInstanceConfig,
    TraderConfig,
)
from harness.core.types import ParquetCodec, QuoteEvent, TradeEvent
from harness.data.alpaca_ws import (
    AlpacaCryptoInbox,
    AlpacaCryptoSubscription,
    AlpacaCryptoWebSocketFeed,
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


def crypto_controls(pairs: list[str]) -> list[object]:
    return [
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "trades": pairs, "quotes": pairs, "bars": []}],
    ]


def crypto_quote(
    timestamp: str = "2024-01-19T14:30:00.123456789Z",
) -> dict[str, object]:
    return {
        "T": "q",
        "S": "BTC/USD",
        "t": timestamp,
        "bx": "CBSE",
        "bp": 43161.69,
        "bs": 0.5,
        "ax": "CBSE",
        "ap": 43162.13,
        "as": 0.028347,
    }


def crypto_trade(
    timestamp: str = "2024-01-19T14:30:00.500000000Z",
) -> dict[str, object]:
    return {
        "T": "t",
        "S": "BTC/USD",
        "i": 133364,
        "x": "CBSE",
        "p": 43161.69,
        "s": 0.028347,
        "t": timestamp,
    }


def crypto_trader_config(
    instances: tuple[tuple[str, ...], ...],
    *,
    feed: str = "crypto",
    network_access: Literal["disabled", "runtime_only"] = "runtime_only",
) -> TraderConfig:
    share = Decimal(1) / len(instances)
    return TraderConfig(
        schema_version=1,
        shell="trader",
        mode="paper",
        seed=1729,
        base_currency="USD",
        strategy_instances=tuple(
            StrategyInstanceConfig(
                instance_id=f"crypto-{index}",
                implementation="strategies.reference.momentum:MomentumStrategy",
                bundle_hash="aa" * 32,
                capital_fraction=share,
                symbols=symbols,
                parameters={},
            )
            for index, symbols in enumerate(instances)
        ),
        risk_pipeline=RiskPipelineConfig(
            on_reject="halt_strategy",
            rules=(
                RiskRuleConfig(
                    rule_id="max-order-notional",
                    implementation="harness.risk.limits:MaxOrderNotional",
                    parameters={"limit": Decimal("10000.00")},
                ),
            ),
        ),
        fees=FeeConfig(
            currency="USD",
            rounding="half_even",
            rules=(
                FeeRuleConfig(
                    fee_id="crypto-commission",
                    basis="per_unit",
                    rate=Decimal("0.005"),
                    minimum=Decimal("0.01"),
                    side="both",
                ),
            ),
        ),
        market_data=RuntimeAdapterConfig(
            adapter="harness.venues.alpaca:AlpacaMarketData",
            credentials={
                "api_key": "${HARNESS_ALPACA_API_KEY}",
                "api_secret": "${HARNESS_ALPACA_API_SECRET}",
            },
            settings={"feed": feed},
        ),
        broker=RuntimeAdapterConfig(
            adapter="harness.venues.alpaca:AlpacaBroker",
            credentials={
                "api_key": "${HARNESS_ALPACA_API_KEY}",
                "api_secret": "${HARNESS_ALPACA_API_SECRET}",
            },
            settings={"account": "paper"},
        ),
        deployment=DeploymentConfig(
            name="trader-local",
            target="local",
            environment="local",
            image=None,
            replicas=1,
            restart_policy="always",
            network_access=network_access,
            resources=ResourceLimitsConfig(cpu_cores=Decimal("1.0"), memory_mib=1024),
        ),
    )


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


def test_crypto_pairs_are_sourced_from_trader_config() -> None:
    config = crypto_trader_config((("ETH/USD", "BTC/USD"), ("BTC/USD",)))
    subscription = AlpacaCryptoSubscription.from_trader_config(config)
    assert subscription.trades == ("BTC/USD", "ETH/USD")
    assert subscription.quotes == ("BTC/USD", "ETH/USD")

    stop = asyncio.Event()
    socket = MockSocket(
        [*crypto_controls(["BTC/USD", "ETH/USD"]), [crypto_trade()]],
        stop=stop,
        stop_after=4,
    )
    server = MockServer([socket])
    inbox = AlpacaCryptoInbox()
    feed = AlpacaCryptoWebSocketFeed.from_trader_config(
        config,
        api_key="key",
        api_secret="secret",
        transport=server,
        inbox=inbox,
        clock=FakeClock(),
    )

    run(feed.run(stop, max_connections=1))

    assert server.urls == ["wss://stream.data.alpaca.markets/v1beta3/crypto"]
    assert socket.sent == [
        {"action": "auth", "key": "key", "secret": "secret"},
        {
            "action": "subscribe",
            "trades": ["BTC/USD", "ETH/USD"],
            "quotes": ["BTC/USD", "ETH/USD"],
        },
    ]
    events = inbox.drain()
    assert len(events) == 1
    assert isinstance(events[0], TradeEvent)
    assert events[0].symbol.value == "BTC/USD"
    assert events[0].event_id.startswith("alpaca-crypto:t:BTC/USD:")
    assert socket.closed


def test_crypto_subscription_requires_pairs_and_crypto_feed_setting() -> None:
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        AlpacaCryptoSubscription(("SPY",), ())
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        AlpacaCryptoSubscription(("BTC/USD/USDT",), ())
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        AlpacaCryptoSubscription(("BTC/",), ())
    with pytest.raises(ValueError, match="at least one crypto"):
        AlpacaCryptoSubscription((), ())
    crypto = AlpacaCryptoSubscription(("BTC/USD",), ("BTC/USD",))
    sip = AlpacaSipSubscription(("BTC/USD",), ("BTC/USD",))
    assert crypto != sip

    with pytest.raises(ValueError, match="must be configured as crypto"):
        AlpacaCryptoSubscription.from_trader_config(
            crypto_trader_config((("BTC/USD",),), feed="sip")
        )
    with pytest.raises(ValueError, match="runtime network access"):
        AlpacaCryptoSubscription.from_trader_config(
            crypto_trader_config((("BTC/USD",),), network_access="disabled")
        )
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        AlpacaCryptoSubscription.from_trader_config(crypto_trader_config((("SPY",),)))


def test_crypto_quotes_and_trades_keep_fractional_decimals_exact() -> None:
    inbox = AlpacaCryptoInbox(initial_sequence=11)
    events = inbox.push(
        [exact(crypto_quote()), exact(crypto_trade())], observed_at=START
    )

    assert isinstance(events[0], QuoteEvent)
    assert events[0].event_id == (
        "alpaca-crypto:q:BTC/USD:2024-01-19T14:30:00.123456+00:00:11"
    )
    assert isinstance(events[0].bid_price, Decimal)
    assert str(events[0].bid_price) == "43161.69"
    assert str(events[0].bid_quantity) == "0.5"
    assert str(events[0].ask_price) == "43162.13"
    assert str(events[0].ask_quantity) == "0.028347"
    assert events[0].venue is None
    assert isinstance(events[1], TradeEvent)
    assert events[1].event_id == (
        "alpaca-crypto:t:BTC/USD:2024-01-19T14:30:00.500000+00:00:12"
    )
    assert str(events[1].price) == "43161.69"
    assert str(events[1].quantity) == "0.028347"
    assert events[1].trade_id == "133364"
    assert events[1].venue == "CBSE"
    for event in events:
        restored = ParquetCodec.decode(ParquetCodec.encode([event]), type(event))[0]
        assert restored == event


def test_crypto_inbox_rejects_non_pair_event_symbols() -> None:
    inbox = AlpacaCryptoInbox()
    message = exact(crypto_quote())
    message["S"] = "SPY"

    with pytest.raises(AlpacaWebSocketProtocolError, match="BASE/QUOTE"):
        inbox.push(message, observed_at=START)

    assert len(inbox) == 0
    assert inbox.next_sequence == 0


def test_crypto_reconnect_across_utc_day_boundary_without_loss_or_duplication() -> None:
    stop = asyncio.Event()
    clock = FakeClock(datetime(2024, 1, 19, 23, 59, 58, tzinfo=UTC))
    first = MockSocket(
        [
            *crypto_controls(["BTC/USD"]),
            [crypto_quote("2024-01-19T23:59:59.123456789Z")],
            ConnectionError("disconnected"),
        ]
    )
    second = MockSocket(
        [
            *crypto_controls(["BTC/USD"]),
            [crypto_trade("2024-01-20T00:00:01.000000000Z")],
        ],
        stop=stop,
        stop_after=4,
    )
    server = MockServer([first, second])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    inbox = AlpacaCryptoInbox()
    feed = AlpacaCryptoWebSocketFeed(
        api_key="key",
        api_secret="secret",
        subscription=AlpacaCryptoSubscription(("BTC/USD",), ("BTC/USD",)),
        transport=server,
        inbox=inbox,
        clock=clock,
        sleep=sleep,
        backoff_seconds=3.0,
    )

    run(feed.run(stop, max_connections=2))

    assert feed.connection_attempts == 2
    assert sleeps == [3.0]
    assert len(server.urls) == 2
    assert first.closed and second.closed
    events = inbox.drain()
    assert [event.sequence for event in events] == [0, 1]
    assert len({event.event_id for event in events}) == 2
    assert isinstance(events[0], QuoteEvent)
    assert events[0].timestamp.date() == date(2024, 1, 19)
    assert isinstance(events[1], TradeEvent)
    assert events[1].timestamp.date() == date(2024, 1, 20)
    assert feed.stale_symbols == ()
