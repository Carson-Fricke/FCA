from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import ExecutionReport, Fill
from harness.core.types.events import Event, QuoteEvent
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.fills.fees import CryptoMakerTakerFeeModel, FeeRounding, RoundingMode
from harness.venues.alpaca_crypto import (
    AlpacaCryptoFeeMismatchError,
    AlpacaCryptoFeeSchedule,
    AlpacaCryptoVenue,
    AlpacaCryptoVenueError,
    DuplicateCryptoOrderError,
    UnknownCryptoOrderError,
)
from harness.venues.base import ExecutionVenue

STAMP = datetime(2024, 1, 5, 23, 30, tzinfo=UTC)
SATURDAY = datetime(2024, 1, 6, 0, 30, tzinfo=UTC)
SUNDAY_NIGHT = datetime(2024, 1, 7, 23, 59, tzinfo=UTC)
BTC = Symbol("BTC/USD")

SCHEDULE_FIXTURE: Mapping[str, object] = {
    "schedule_id": "alpaca-crypto-venue",
    "currency": "USD",
    "maker_rate_bps": "15.00",
    "taker_rate_bps": "25.00",
    "rounding": {"precision": "0.01", "mode": "half_even"},
}


def quote(
    event_id: str,
    offset: int = 0,
    sequence: int = 0,
    *,
    bid: str = "49999.50",
    ask: str = "50000.00",
    bid_quantity: str = "5",
    ask_quantity: str = "5",
    stamp: datetime = STAMP,
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        stamp + timedelta(microseconds=offset),
        sequence,
        BTC,
        Decimal(bid),
        Decimal(bid_quantity),
        Decimal(ask),
        Decimal(ask_quantity),
    )


def order(
    order_id: str,
    side: Side,
    order_type: OrderType,
    *,
    symbol: str = BTC,
    quantity: str = "0.25",
    tif: TimeInForce = TimeInForce.GTC,
    limit_price: str | None = None,
    stop_price: str | None = None,
    offset: int = 0,
    stamp: datetime = STAMP,
) -> SingleLegOrder:
    return SingleLegOrder(
        f"event-{order_id}",
        stamp + timedelta(microseconds=offset),
        0,
        order_id,
        Symbol(symbol),
        side,
        Decimal(quantity),
        order_type,
        tif,
        None if limit_price is None else Decimal(limit_price),
        None if stop_price is None else Decimal(stop_price),
    )


def fee_model(
    *,
    maker: str = "15",
    taker: str = "25",
    precision: str = "0.01",
    mode: RoundingMode = RoundingMode.HALF_EVEN,
) -> CryptoMakerTakerFeeModel:
    return CryptoMakerTakerFeeModel(
        rounding=FeeRounding(Decimal(precision), mode),
        maker_rate_bps=Decimal(maker),
        taker_rate_bps=Decimal(taker),
    )


def fee_schedule(
    *,
    maker: str = "15",
    taker: str = "25",
    precision: str = "0.01",
    mode: RoundingMode = RoundingMode.HALF_EVEN,
) -> AlpacaCryptoFeeSchedule:
    return AlpacaCryptoFeeSchedule(
        schedule_id="alpaca-crypto-fixture",
        currency="USD",
        maker_rate_bps=Decimal(maker),
        taker_rate_bps=Decimal(taker),
        rounding=FeeRounding(Decimal(precision), mode),
    )


def market_and_venue(
    *,
    model: CryptoMakerTakerFeeModel | None = None,
    schedule: AlpacaCryptoFeeSchedule | None = None,
    latency: timedelta = timedelta(0),
    clock_start: datetime = STAMP,
) -> tuple[EventBus, AlpacaCryptoVenue, list[Event]]:
    bus = EventBus(SimClock(clock_start))
    venue = AlpacaCryptoVenue(
        bus,
        fee_model=model if model is not None else fee_model(),
        fee_schedule=schedule if schedule is not None else fee_schedule(),
        latency=latency,
    )
    seen: list[Event] = []
    bus.subscribe(ExecutionReport, seen.append)
    bus.subscribe(Fill, seen.append)
    return bus, venue, seen


def test_fractional_round_trip_maps_reports_and_fills_exactly() -> None:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("q1", sequence=0))
    bus.drain()
    venue.submit(
        order("buy", Side.BUY, OrderType.LIMIT, quantity="0.25", limit_price="50000.00")
    )
    venue.submit(
        order(
            "sell",
            Side.SELL,
            OrderType.LIMIT,
            quantity="0.25",
            limit_price="49999.50",
        )
    )
    bus.drain()

    assert isinstance(venue, ExecutionVenue)
    reports = [event for event in seen if isinstance(event, ExecutionReport)]
    fills = [event for event in seen if isinstance(event, Fill)]
    assert [report.execution_type for report in reports] == [
        ExecutionType.ACKNOWLEDGED,
        ExecutionType.FILL,
        ExecutionType.ACKNOWLEDGED,
        ExecutionType.FILL,
    ]
    buy_report = reports[1]
    assert buy_report.order_quantity == Decimal("0.25")
    assert buy_report.filled_quantity == Decimal("0.25")
    assert buy_report.remaining_quantity == Decimal("0")
    assert buy_report.average_fill_price == Decimal("50000")
    assert [(fill.side, fill.quantity, fill.price) for fill in fills] == [
        (Side.BUY, Decimal("0.25"), Decimal("50000.00")),
        (Side.SELL, Decimal("0.25"), Decimal("49999.50")),
    ]
    assert [fill.fee for fill in fills] == [Decimal("31.25"), Decimal("31.25")]
    assert venue.state("buy").fees_paid == Decimal("31.25")
    assert venue.state("sell").fees_paid == Decimal("31.25")
    signed = sum(
        (fill.quantity if fill.side is Side.BUY else -fill.quantity for fill in fills),
        Decimal("0"),
    )
    assert signed == 0


def test_fractional_partial_fills_stay_exact() -> None:
    bus, venue, seen = market_and_venue()
    venue.submit(
        order(
            "partial",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.75",
            limit_price="50000.00",
        )
    )
    bus.publish(quote("thin", sequence=1, ask="50000.00", ask_quantity="0.3"))
    bus.drain()

    assert [fill.quantity for fill in seen if isinstance(fill, Fill)] == [
        Decimal("0.3")
    ]
    state = venue.state("partial")
    assert state.status is OrderStatus.PARTIALLY_FILLED
    assert state.remaining_quantity == Decimal("0.45")

    bus.publish(
        quote("rest", offset=1, sequence=2, ask="50000.00", ask_quantity="0.45")
    )
    bus.drain()

    state = venue.state("partial")
    assert state.status is OrderStatus.FILLED
    assert state.filled_quantity == Decimal("0.75")
    assert state.remaining_quantity == Decimal("0")
    assert state.fees_paid == Decimal("56.25")
    fills = [fill for fill in seen if isinstance(fill, Fill)]
    assert sum((fill.quantity for fill in fills), Decimal("0")) == Decimal("0.75")


def test_cancellation_and_fractional_replacement_map_exactly() -> None:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("q1", sequence=0))
    bus.drain()

    venue.submit(
        order(
            "cancel",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.5",
            limit_price="49000.00",
        )
    )
    bus.drain()
    venue.cancel("cancel")
    bus.drain()
    assert venue.state("cancel").status is OrderStatus.CANCELED

    venue.submit(
        order(
            "original",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.5",
            limit_price="49000.00",
        )
    )
    bus.drain()
    replacement = order(
        "repl",
        Side.BUY,
        OrderType.LIMIT,
        quantity="0.125",
        limit_price="50000.00",
        offset=1,
    )
    venue.replace("original", replacement)
    assert venue.state("original").status is OrderStatus.REPLACED
    bus.drain()

    assert venue.state("repl").status is OrderStatus.FILLED
    fills = [fill for fill in seen if isinstance(fill, Fill)]
    assert [(fill.order_id, fill.quantity) for fill in fills] == [
        ("repl", Decimal("0.125"))
    ]
    assert fills[0].fee == Decimal("15.62")
    assert venue.state("repl").fees_paid == Decimal("15.62")
    replaced = [
        report
        for report in seen
        if isinstance(report, ExecutionReport)
        and report.execution_type is ExecutionType.REPLACED
    ]
    assert [report.order_id for report in replaced] == ["original"]


@pytest.mark.parametrize(
    "weekend_stamp",
    [SATURDAY, SUNDAY_NIGHT],
)
def test_continuous_sessions_accept_orders_on_every_utc_day(
    weekend_stamp: datetime,
) -> None:
    assert AlpacaCryptoVenue.continuous is True
    bus, venue, seen = market_and_venue(clock_start=weekend_stamp)
    bus.publish(quote("weekend", stamp=weekend_stamp))
    bus.drain()
    venue.submit(
        order(
            "always-on",
            Side.BUY,
            OrderType.MARKET,
            quantity="0.01",
            stamp=weekend_stamp,
        )
    )
    bus.drain()

    fills = [event for event in seen if isinstance(event, Fill)]
    assert [(fill.quantity, fill.price) for fill in fills] == [
        (Decimal("0.01"), Decimal("50000.00"))
    ]
    assert fills[0].timestamp == weekend_stamp
    reports = [event for event in seen if isinstance(event, ExecutionReport)]
    assert [report.status for report in reports] == [
        OrderStatus.ACCEPTED,
        OrderStatus.FILLED,
    ]


def test_crossing_orders_take_and_resting_orders_make_liquidity() -> None:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("q1", sequence=0))
    bus.drain()
    venue.submit(
        order(
            "taker", Side.BUY, OrderType.LIMIT, quantity="0.25", limit_price="50000.00"
        )
    )
    bus.drain()
    venue.submit(
        order(
            "maker",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.25",
            limit_price="49990.00",
        )
    )
    bus.drain()
    bus.publish(quote("q2", offset=1, sequence=1, ask="49990.00", ask_quantity="0.25"))
    bus.drain()

    fills = [event for event in seen if isinstance(event, Fill)]
    assert [(fill.order_id, fill.liquidity, fill.price) for fill in fills] == [
        ("taker", "taker", Decimal("50000.00")),
        ("maker", "maker", Decimal("49990.00")),
    ]
    assert [fill.fee for fill in fills] == [Decimal("31.25"), Decimal("18.75")]


def test_fee_ledger_matches_venue_reported_charges() -> None:
    schedule = fee_schedule()
    bus, venue, seen = market_and_venue(schedule=schedule)
    bus.publish(quote("q1", sequence=0))
    bus.drain()
    venue.submit(
        order(
            "taker", Side.BUY, OrderType.LIMIT, quantity="0.25", limit_price="50000.00"
        )
    )
    venue.submit(
        order(
            "maker",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.25",
            limit_price="49990.00",
        )
    )
    bus.drain()
    bus.publish(quote("q2", offset=1, sequence=1, ask="49990.00", ask_quantity="0.25"))
    bus.drain()

    entries = venue.fee_ledger
    fills = [event for event in seen if isinstance(event, Fill)]
    assert [entry.fill_id for entry in entries] == [fill.fill_id for fill in fills]
    for entry, fill in zip(entries, fills, strict=True):
        reported = schedule.charge(entry.quantity, entry.price, entry.liquidity)
        assert entry.fee == fill.fee == reported
        assert venue.state(entry.order_id).fees_paid == entry.fee


def test_fee_rate_mismatch_is_an_explicit_error_on_the_command_path() -> None:
    bus, venue, seen = market_and_venue(schedule=fee_schedule(taker="30"))
    bus.publish(quote("q1", sequence=0, ask="30000.00"))
    bus.drain()
    crossing = order(
        "cross",
        Side.BUY,
        OrderType.LIMIT,
        quantity="0.123",
        limit_price="30000.00",
    )
    with pytest.raises(AlpacaCryptoFeeMismatchError, match="fee mismatch"):
        venue.submit(crossing)
    assert not [event for event in seen if isinstance(event, Fill)]


def test_fee_rounding_mismatch_is_an_explicit_error_on_the_response_path() -> None:
    bus, venue, seen = market_and_venue(
        schedule=fee_schedule(mode=RoundingMode.CEILING)
    )
    venue.submit(
        order(
            "rest",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.001",
            limit_price="30000.00",
        )
    )
    bus.drain()
    assert not [event for event in seen if isinstance(event, Fill)]

    bus.publish(
        quote("touch", offset=1, sequence=1, ask="30000.00", ask_quantity="0.001")
    )
    with pytest.raises(AlpacaCryptoFeeMismatchError, match="0.04"):
        bus.drain()
    assert not [event for event in seen if isinstance(event, Fill)]


def test_ioc_orders_without_a_touch_expire_through_the_bus() -> None:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("q1", sequence=0))
    bus.drain()
    ioc = order(
        "ioc",
        Side.BUY,
        OrderType.LIMIT,
        quantity="0.25",
        tif=TimeInForce.IOC,
        limit_price="40000.00",
    )
    venue.submit(ioc)
    bus.drain()

    reports = [event for event in seen if isinstance(event, ExecutionReport)]
    assert [report.status for report in reports] == [
        OrderStatus.ACCEPTED,
        OrderStatus.EXPIRED,
    ]
    assert venue.state("ioc").status is OrderStatus.EXPIRED


@pytest.mark.parametrize("symbol", ["AAPL", "BTCUSD", "BTC/USD/X"])
def test_non_crypto_pair_symbols_are_rejected(symbol: str) -> None:
    _, venue, _ = market_and_venue()
    with pytest.raises(AlpacaCryptoVenueError, match="BASE/QUOTE"):
        venue.submit(
            order(
                "bad",
                Side.BUY,
                OrderType.LIMIT,
                symbol=symbol,
                limit_price="100",
            )
        )


def test_stop_order_types_are_rejected() -> None:
    _, venue, _ = market_and_venue()
    with pytest.raises(AlpacaCryptoVenueError, match="market and limit"):
        venue.submit(order("stop", Side.BUY, OrderType.STOP, stop_price="50000"))


def test_duplicate_order_identifiers_are_rejected() -> None:
    _, venue, _ = market_and_venue()
    venue.submit(
        order("dup", Side.BUY, OrderType.LIMIT, quantity="0.25", limit_price="60000")
    )
    with pytest.raises(DuplicateCryptoOrderError, match="dup"):
        venue.submit(
            order(
                "dup", Side.BUY, OrderType.LIMIT, quantity="0.25", limit_price="60000"
            )
        )


def test_unknown_order_commands_are_rejected() -> None:
    _, venue, _ = market_and_venue()
    with pytest.raises(UnknownCryptoOrderError):
        venue.cancel("nope")
    with pytest.raises(UnknownCryptoOrderError):
        venue.state("nope")


def test_commands_do_not_inline_outcomes_or_allow_pending_races() -> None:
    bus, venue, seen = market_and_venue(latency=timedelta(microseconds=1))
    venue.submit(order("pending", Side.BUY, OrderType.MARKET, quantity="0.1"))
    bus.publish(quote("q", sequence=1))
    bus.dispatch_next()
    assert seen == []
    with pytest.raises(AlpacaCryptoVenueError, match="pending"):
        venue.cancel("pending")


def test_position_snapshot_command_validates_account() -> None:
    _, venue, _ = market_and_venue()
    venue.request_position_snapshot("acct-1")
    with pytest.raises(ValueError):
        venue.request_position_snapshot("   ")


def test_fee_schedule_builds_from_a_strict_fixture_mapping() -> None:
    schedule = AlpacaCryptoFeeSchedule.from_mapping(SCHEDULE_FIXTURE)
    assert schedule.schedule_id == "alpaca-crypto-venue"
    assert schedule.currency == "USD"
    assert schedule.rate_for("maker") == Decimal("15.00")
    assert schedule.rate_for("taker") == Decimal("25.00")
    assert schedule.charge(Decimal("0.25"), Decimal("50000"), "taker") == Decimal(
        "31.25"
    )


def test_from_mapping_requires_every_field() -> None:
    values = dict(SCHEDULE_FIXTURE)
    del values["currency"]
    with pytest.raises(ValueError, match="missing"):
        AlpacaCryptoFeeSchedule.from_mapping(values)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"taker_rate_bps": 25.0}, TypeError),
        ({"currency": "usd"}, ValueError),
        ({"venue": "alpaca"}, ValueError),
        (
            {
                "rounding": {
                    "precision": "0.015",
                    "mode": "half_even",
                }
            },
            ValueError,
        ),
        ({"rounding": {"precision": "0.01", "mode": "nope"}}, ValueError),
        ({"rounding": {"precision": 0.01, "mode": "half_even"}}, TypeError),
    ],
)
def test_from_mapping_rejects_invalid_fields(
    override: Mapping[str, object], error: type[Exception]
) -> None:
    values = dict(SCHEDULE_FIXTURE)
    values.update(override)
    with pytest.raises(error):
        AlpacaCryptoFeeSchedule.from_mapping(values)


def _fractional_scenario() -> tuple[list[type[Event]], str]:
    bus, venue, seen = market_and_venue()
    bus.publish(quote("q1", sequence=0))
    bus.drain()
    venue.submit(
        order("buy", Side.BUY, OrderType.LIMIT, quantity="0.25", limit_price="50000.00")
    )
    venue.submit(
        order(
            "rest",
            Side.BUY,
            OrderType.LIMIT,
            quantity="0.5",
            limit_price="49990.00",
        )
    )
    bus.drain()
    bus.publish(quote("q2", offset=1, sequence=1, ask="49990.00", ask_quantity="0.25"))
    bus.drain()
    venue.cancel("rest")
    bus.drain()
    return [type(event) for event in seen], bus.dispatch_hash


def test_identical_scenarios_dispatch_identically() -> None:
    assert _fractional_scenario() == _fractional_scenario()
