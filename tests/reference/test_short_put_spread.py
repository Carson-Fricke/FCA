"""Integration tests for the reference short put spread strategy.

Each scenario replays deterministic synthetic option quotes through a
hand-composed kernel -- SimClock, EventBus, PortfolioHub, the point-in-time
option universe, the atomic OptionsMatchingEngine with its MultiLegOMS, and
the strategy binding -- mirroring the reference equity strategy integration
tests.  The backtest runner does not yet wire a multi-leg venue, so the
``_OptionsVenueAdapter`` below stands in for the runner-owned venue adapter
and maps strategy limit groups to net-credit or net-debit by the first
leg's side.

Quote prices are per-contract notional values (per-share bid/ask scaled by
the contract multiplier), matching the documented options-lifecycle
convention, so all golden money values are exact contract dollars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from strategies.harness_examples.short_put_spread import (
    ShortPutSpreadStrategy,
)

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import (
    Event,
    EventType,
    ExecutionReport,
    Fill,
    MultiLegOrder,
    OrderRequest,
    OrderType,
    QuoteEvent,
    Side,
    TradeEvent,
)
from harness.data.options_universe import (
    DteFilter,
    OptionUniverseSnapshot,
    OptionUniverseSpec,
    OptionUniverseView,
    SelectionMode,
)
from harness.fills.mid_touch import MidTouch
from harness.oms.groups import GroupOrderType, GroupStatus, MultiLegOMS
from harness.portfolio import PortfolioHub
from harness.portfolio.options_lifecycle import OptionLifecycle, SettlementKind
from harness.strategy import (
    EmissionSink,
    StrategyContext,
    assert_sandbox_clean,
    bind_strategy,
    lint_strategy_directory,
)
from harness.venues import OptionsMatchingEngine
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryDailyFrontSetRepository,
)
from lake.catalog.memory import InMemoryContractRepository
from lake.catalog.models import ContractRecord, OptionType

REPO_ROOT = Path(__file__).resolve().parents[2]
UNDERLYING = "SPY"
EXPIRY = date(2024, 1, 19)
ENTRY_DAY = date(2024, 1, 16)
MANAGED_DAY = date(2024, 1, 18)
SHORT_OSI = "SPY240119P00500000"
LONG_OSI = "SPY240119P00495000"
FEB_OSI = "SPY240216P00500000"
MULTIPLIER = 100
BOOK_ID = "short-put-spread"
INITIAL_CASH = Decimal("100000.00")
_QUOTE_SIZE = "50"
_SCALE = Decimal(MULTIPLIER)


def _session_stamp(day: date, microsecond: int) -> datetime:
    """Return one deterministic session timestamp on ``day``."""
    return datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC) + timedelta(
        microseconds=microsecond
    )


def _quote(
    symbol: str,
    day: date,
    microsecond: int,
    sequence: int,
    *,
    bid: str,
    ask: str,
) -> QuoteEvent:
    """Build one quote from per-share bid/ask inputs, scaled to contracts."""
    return QuoteEvent(
        event_id=f"{symbol}-{sequence}",
        timestamp=_session_stamp(day, microsecond),
        sequence=sequence,
        symbol=Symbol(symbol),
        bid_price=Decimal(bid) * _SCALE,
        bid_quantity=Decimal(_QUOTE_SIZE),
        ask_price=Decimal(ask) * _SCALE,
        ask_quantity=Decimal(_QUOTE_SIZE),
    )


def _round(
    day: date,
    sequence: int,
    microsecond: int,
    *,
    short: tuple[str, str],
    long_: tuple[str, str],
) -> tuple[QuoteEvent, QuoteEvent]:
    """Build one quote round: the short leg first, then the long leg."""
    return (
        _quote(SHORT_OSI, day, microsecond, sequence, bid=short[0], ask=short[1]),
        _quote(
            LONG_OSI, day, microsecond + 2, sequence + 1, bid=long_[0], ask=long_[1]
        ),
    )


def _entry_round() -> tuple[QuoteEvent, QuoteEvent]:
    """Quotes that fill the entry at a 50.00 per-contract credit."""
    return _round(ENTRY_DAY, 0, 1, short=("0.98", "1.02"), long_=("0.48", "0.52"))


def _profit_quotes() -> tuple[QuoteEvent, ...]:
    """Entry round plus a compressed round that trips the profit exit."""
    return _entry_round() + _round(
        ENTRY_DAY,
        2,
        1_000_001,
        short=("0.28", "0.32"),
        long_=("0.04", "0.06"),
    )


def _contract(symbol: str, strike: str, expiry: date) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying=UNDERLYING,
        expiration_date=expiry,
        strike_price=Decimal(strike),
        option_type=OptionType.PUT,
        multiplier=MULTIPLIER,
        listed_date=date(2024, 1, 2),
    )


_SHORT_CONTRACT = _contract(SHORT_OSI, "500", EXPIRY)
_LONG_CONTRACT = _contract(LONG_OSI, "495", EXPIRY)
_FEB_CONTRACT = _contract(FEB_OSI, "500", date(2024, 2, 16))


def _catalog() -> ContractsCatalog:
    """Return a catalog whose daily front sets expose only the spread legs."""
    front_sets = tuple(
        DailyFrontSet(
            underlying=UNDERLYING,
            selection_date=day,
            contracts=(_SHORT_CONTRACT, _LONG_CONTRACT),
        )
        for day in (ENTRY_DAY, MANAGED_DAY, EXPIRY)
    )
    return ContractsCatalog(
        InMemoryContractRepository((_SHORT_CONTRACT, _LONG_CONTRACT, _FEB_CONTRACT)),
        InMemoryDailyFrontSetRepository(front_sets),
    )


class _UniverseData:
    """Point-in-time adapter exposing the option universe via ``context.data``."""

    def __init__(self, view: OptionUniverseView, underlying: str) -> None:
        self._view = view
        self._underlying = Symbol(underlying)

    def read(
        self,
        symbol: str | Symbol,
        data_type: type[Event] | EventType | None = None,
    ) -> object:
        if Symbol(str(symbol)) != self._underlying:
            return None
        return self._view.resolve()


class _OptionsVenueAdapter:
    """ExecutionVenue adapter over the atomic options matching engine.

    The backtest runner does not yet wire a multi-leg venue, so this adapter
    stands in for the runner-owned integration: limit groups map to
    NET_CREDIT or NET_DEBIT by their first leg's side, and market groups map
    to MARKET.
    """

    def __init__(self, engine: OptionsMatchingEngine) -> None:
        self._engine = engine

    def submit(self, order: OrderRequest) -> None:
        if not isinstance(order, MultiLegOrder):
            raise TypeError("the options venue accepts multi-leg orders only")
        if order.order_type is OrderType.MARKET:
            self._engine.submit(order, GroupOrderType.MARKET)
            return
        group_type = (
            GroupOrderType.NET_CREDIT
            if order.legs[0].side is Side.SELL
            else GroupOrderType.NET_DEBIT
        )
        self._engine.submit(order, group_type)

    def cancel(self, order_id: str) -> None:
        self._engine.cancel(order_id)

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        raise RuntimeError("the options venue does not support replacement")

    def request_position_snapshot(self, account_id: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Run:
    """One completed replay with every deterministic artifact tests assert on."""

    strategy: ShortPutSpreadStrategy
    fills: tuple[Fill, ...]
    venue: OptionsMatchingEngine
    oms: MultiLegOMS
    hub: PortfolioHub
    emissions: EmissionSink
    data: _UniverseData


def _run(
    quotes: tuple[QuoteEvent, ...],
    *,
    parameters: dict[str, object] | None = None,
) -> _Run:
    """Replay ``quotes`` through the composed kernel and return its artifacts."""
    clock = SimClock(quotes[0].timestamp)
    bus = EventBus(clock)
    hub = PortfolioHub("short-put-spread-reference")
    hub.create_book(BOOK_ID, INITIAL_CASH)
    view = OptionUniverseView(
        clock,
        _catalog(),
        OptionUniverseSpec(
            underlying=UNDERLYING,
            mode=SelectionMode.FRONT_SET,
            dte=DteFilter(min_days=0, max_days=14),
        ),
    )
    data = _UniverseData(view, UNDERLYING)
    oms = MultiLegOMS()
    engine = OptionsMatchingEngine(
        bus, MidTouch(), timedelta(0), clock=clock, venue="options", oms=oms
    )
    emissions = EmissionSink()
    context = StrategyContext(
        clock,
        portfolio=hub,
        venue=_OptionsVenueAdapter(engine),
        bus=bus,
        data=data,
        universe=(UNDERLYING, SHORT_OSI, LONG_OSI),
        subscriptions=(QuoteEvent, TradeEvent, ExecutionReport, Fill),
        book_id=BOOK_ID,
        emissions=emissions,
    )
    values = {"expiration": EXPIRY.isoformat(), **(parameters or {})}
    binding = bind_strategy(ShortPutSpreadStrategy, values, context)
    fills: list[Fill] = []
    bus.subscribe(Fill, fills.append)
    binding.start()
    for quote in quotes:
        bus.publish(quote)
        bus.drain()
    binding.stop()
    strategy = cast(ShortPutSpreadStrategy, binding.strategy)
    return _Run(strategy, tuple(fills), engine, oms, hub, emissions, data)


def _emission_fields(run: _Run, name: str) -> dict[str, object]:
    return {
        key: value
        for record in run.emissions.records
        if record.name == name
        for key, value in dict(record.fields).items()
    }


def test_reference_options_strategy_is_sandbox_clean() -> None:
    directory_report = lint_strategy_directory(REPO_ROOT / "strategies")
    assert directory_report.ok, directory_report.render()

    report = assert_sandbox_clean(
        REPO_ROOT / "strategies" / "harness_examples" / "short_put_spread.py"
    )
    assert report.ok


def test_short_put_spread_enters_and_takes_profit() -> None:
    run = _run(_profit_quotes())

    assert run.strategy.phase == "flat"
    assert tuple(run.strategy.callbacks) == (
        "on_start",
        "on_quote",
        "on_quote",
        "on_fill",
        "on_fill",
        "on_quote",
        "on_quote",
        "on_fill",
        "on_fill",
        "on_stop",
    )
    assert [
        (fill.order_id, str(fill.symbol), fill.side.value, fill.price)
        for fill in run.fills
    ] == [
        ("sps-0001", SHORT_OSI, "sell", Decimal("100")),
        ("sps-0001", LONG_OSI, "buy", Decimal("50")),
        ("sps-0002", SHORT_OSI, "buy", Decimal("30")),
        ("sps-0002", LONG_OSI, "sell", Decimal("5")),
    ]
    assert all(fill.liquidity == "maker" for fill in run.fills)

    entry = run.oms.state("sps-0001")
    assert entry.status is GroupStatus.FILLED
    assert entry.net_price == Decimal("-50")
    assert entry.net_cash == Decimal("50")
    redeem = run.oms.state("sps-0002")
    assert redeem.status is GroupStatus.FILLED
    assert redeem.net_price == Decimal("25")
    assert redeem.net_cash == Decimal("-25")

    book = run.hub.book(BOOK_ID)
    assert book.fill_count == 4
    assert book.cash == Decimal("100025.00")
    assert book.position(SHORT_OSI).quantity == Decimal("0")
    assert book.position(LONG_OSI).quantity == Decimal("0")
    assert book.realized_pnl == Decimal("25")

    assert [record.name for record in run.emissions.records] == [
        "short_put_spread_entry",
        "short_put_spread_exit",
        "short_put_spread_summary",
    ]
    entry_fields = _emission_fields(run, "short_put_spread_entry")
    assert entry_fields["short_symbol"] == SHORT_OSI
    assert entry_fields["long_symbol"] == LONG_OSI
    assert entry_fields["entry_credit"] == Decimal("50")
    assert entry_fields["collateral_bound"] == Decimal("450")
    exit_fields = _emission_fields(run, "short_put_spread_exit")
    assert exit_fields["exit_debit"] == Decimal("25")
    assert exit_fields["realized_pnl"] == Decimal("25")
    summary_fields = _emission_fields(run, "short_put_spread_summary")
    assert summary_fields["phase"] == "flat"
    assert summary_fields["filled_groups"] == 2


def test_front_set_selection_is_point_in_time_and_exclusive() -> None:
    run = _run(_profit_quotes())

    snapshot = run.data.read(UNDERLYING)
    assert isinstance(snapshot, OptionUniverseSnapshot)
    assert snapshot.symbols == (LONG_OSI, SHORT_OSI)
    assert FEB_OSI not in snapshot.symbols
    assert snapshot.dte(EXPIRY) == 3


def test_unfilled_entry_cancels_inside_the_cancel_window() -> None:
    quotes = _round(
        ENTRY_DAY, 0, 1, short=("0.92", "0.96"), long_=("0.49", "0.51")
    ) + _round(MANAGED_DAY, 2, 1, short=("0.92", "0.96"), long_=("0.49", "0.51"))
    run = _run(quotes, parameters={"cancel_dte": 1})

    assert run.strategy.phase == "canceled_no_fill"
    assert tuple(run.strategy.callbacks) == (
        "on_start",
        "on_quote",
        "on_quote",
        "on_quote",
        "on_quote",
        "on_stop",
    )
    assert run.fills == ()
    assert run.oms.state("sps-0001").status is GroupStatus.CANCELED
    assert run.venue.state("sps-0001").status is GroupStatus.CANCELED
    assert run.hub.book(BOOK_ID).cash == INITIAL_CASH
    assert run.strategy.entry_credit is None


def test_stop_exit_redeems_when_the_mark_reaches_the_stop_multiple() -> None:
    quotes = _entry_round() + _round(
        ENTRY_DAY,
        2,
        1_000_001,
        short=("1.80", "1.82"),
        long_=("0.78", "0.80"),
    )
    run = _run(quotes)

    assert run.strategy.phase == "flat"
    assert run.strategy.realized_pnl == Decimal("-52")
    redeem = run.oms.state("sps-0002")
    assert redeem.status is GroupStatus.FILLED
    assert redeem.net_price == Decimal("102")
    assert run.hub.book(BOOK_ID).cash == Decimal("99948.00")


def test_managed_exit_redeems_at_the_configured_dte() -> None:
    quotes = _entry_round() + _round(
        MANAGED_DAY, 2, 1, short=("0.58", "0.62"), long_=("0.18", "0.22")
    )
    run = _run(quotes, parameters={"exit_dte": 1})

    assert run.strategy.phase == "flat"
    assert run.strategy.realized_pnl == Decimal("10")
    redeem = run.oms.state("sps-0002")
    assert redeem.status is GroupStatus.FILLED
    assert redeem.net_price == Decimal("40")
    assert run.hub.book(BOOK_ID).cash == Decimal("100010.00")


def test_expiry_day_residual_value_liquidates_at_market() -> None:
    quotes = _entry_round() + _round(
        EXPIRY, 2, 1, short=("0.55", "0.59"), long_=("0.05", "0.07")
    )
    run = _run(quotes)

    assert run.strategy.phase == "flat"
    assert [(fill.order_id, fill.side.value, fill.price) for fill in run.fills[2:]] == [
        ("sps-0002", "buy", Decimal("59")),
        ("sps-0002", "sell", Decimal("5")),
    ]
    assert all(fill.liquidity == "taker" for fill in run.fills[2:])
    assert run.strategy.realized_pnl == Decimal("-4")
    assert run.hub.book(BOOK_ID).cash == Decimal("99996.00")


def test_expiry_day_zero_mark_leaves_contracts_to_shell_settlement() -> None:
    quotes = _entry_round() + _round(
        EXPIRY, 2, 1, short=("0.04", "0.06"), long_=("0.04", "0.06")
    )
    run = _run(quotes)

    assert run.strategy.phase == "awaiting_settlement"
    assert len(run.fills) == 2
    assert run.hub.book(BOOK_ID).cash == Decimal("100050.00")
    assert "short_put_spread_expiry" in [
        record.name for record in run.emissions.records
    ]

    lifecycle = OptionLifecycle(run.hub, SimClock(quotes[-1].timestamp))
    settlements = lifecycle.settle_expired({UNDERLYING: Decimal("520.00")})

    assert [settlement.kind for settlement in settlements] == [
        SettlementKind.WORTHLESS,
        SettlementKind.WORTHLESS,
    ]
    assert all(settlement.contracts == Decimal("0") for settlement in settlements)
    assert all(settlement.underlying_fill is None for settlement in settlements)
    assert lifecycle.is_retired(SHORT_OSI)
    assert lifecycle.is_retired(LONG_OSI)
    book = run.hub.book(BOOK_ID)
    assert book.position(SHORT_OSI).quantity == Decimal("0")
    assert book.position(LONG_OSI).quantity == Decimal("0")
    assert book.cash == Decimal("100050.00")


def test_short_put_spread_replays_identically() -> None:
    first = _run(_profit_quotes())
    second = _run(_profit_quotes())

    assert first.strategy.callbacks == second.strategy.callbacks
    assert first.fills == second.fills
    assert first.emissions.records == second.emissions.records
    assert first.oms.state("sps-0001") == second.oms.state("sps-0001")
    assert first.oms.state("sps-0002") == second.oms.state("sps-0002")
    assert first.venue.state("sps-0001") == second.venue.state("sps-0001")
    assert first.hub.book(BOOK_ID).cash == second.hub.book(BOOK_ID).cash
    assert first.hub.book(BOOK_ID).realized_pnl == second.hub.book(BOOK_ID).realized_pnl
