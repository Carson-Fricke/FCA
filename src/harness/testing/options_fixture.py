"""Deterministic in-memory options fixture for the strategy plugin surface.

The Python engine was deleted with the Rust migration; the native ``host`` owns
the live matching, portfolio, and option-lifecycle kernel.  The plugin surface
still needs a way to exercise the reference options strategies deterministically
in CI, so this module keeps a deliberately small, engine-free options kernel in
:mod:`harness.testing`.  It models exactly the seams the strategy contract
exposes:

* a point-in-time :class:`~harness.data.options_universe.OptionUniverseView`
  over an in-memory ``ContractsCatalog``;
* a read-only engine clock that advances only from the packaged quote stream;
* an atomic multi-leg order sink with net-credit, net-debit, and market
  matching;
* a per-book cash/position ledger with average-cost realized PnL; and
* option expiry settlement producing worthless, assigned, or exercised
  dispositions with exact underlying fills at the strike.

No code path reads wall time, draws unseeded randomness, or touches the
network.  The per-context generator is seeded at construction, so replaying the
same fixture always produces identical fills and emissions.  The package also
ships the canonical short-put-spread scenario (:data:`SPS_SHORT_OSI`,
:data:`SPS_LONG_OSI`, and the ``sps_*`` quote builders) that the reference
strategy test runs over.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from random import Random
from typing import TYPE_CHECKING, Any, Final, cast

from harness.core.clock import SimClock
from harness.core.symbols import Symbol
from harness.core.types import (
    Fill,
    MultiLegOrder,
    OrderLeg,
    OrderType,
    QuoteEvent,
    Side,
)
from harness.data.options_universe import (
    DteFilter,
    OptionUniverseSnapshot,
    OptionUniverseSpec,
    OptionUniverseView,
    SelectionMode,
)
from harness.strategy.contracts import Strategy, StrategyContext
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryDailyFrontSetRepository,
)
from lake.catalog.memory import InMemoryContractRepository
from lake.catalog.models import ContractRecord, OptionType

if TYPE_CHECKING:
    from collections.abc import Iterable

_ZERO: Final = Decimal("0")
_TWO: Final = Decimal("2")
_DEFAULT_CASH: Final = Decimal("100000.00")
_DEFAULT_SEED: Final = 7
_SESSION_HOUR: Final = 14
_SESSION_MINUTE: Final = 30

SPS_UNDERLYING: Final = "SPY"
SPS_MULTIPLIER: Final = 100
SPS_EXPIRY: Final = date(2024, 1, 19)
SPS_ENTRY_DAY: Final = date(2024, 1, 16)
SPS_MANAGED_DAY: Final = date(2024, 1, 18)
SPS_SHORT_OSI: Final = "SPY240119P00500000"
SPS_LONG_OSI: Final = "SPY240119P00495000"
SPS_FEB_OSI: Final = "SPY240216P00500000"
SPS_SHORT_STRIKE: Final = Decimal("500")
SPS_LONG_STRIKE: Final = Decimal("495")


class SettlementKind(StrEnum):
    """How one option position ended at expiration settlement."""

    WORTHLESS = "worthless"
    ASSIGNED = "assigned"
    EXERCISED = "exercised"


@dataclass(frozen=True, slots=True)
class FixtureQuote:
    """One deterministic top-of-book option quote at a session instant."""

    symbol: Symbol
    timestamp: datetime
    sequence: int
    bid_price: Decimal
    ask_price: Decimal
    bid_quantity: Decimal = Decimal("100")
    ask_quantity: Decimal = Decimal("100")

    def event(self) -> QuoteEvent:
        """Return the immutable quote event delivered to the strategy."""
        return QuoteEvent(
            event_id=f"{self.symbol}-{self.sequence}",
            timestamp=self.timestamp,
            sequence=self.sequence,
            symbol=self.symbol,
            bid_price=self.bid_price,
            bid_quantity=self.bid_quantity,
            ask_price=self.ask_price,
            ask_quantity=self.ask_quantity,
        )

    @property
    def mid(self) -> Decimal:
        """Return the exact quote midpoint."""
        return (self.bid_price + self.ask_price) / _TWO


@dataclass(frozen=True, slots=True)
class FixtureSettlement:
    """The settlement outcome for one option position at expiration."""

    symbol: str
    kind: SettlementKind
    contracts: Decimal
    spot: Decimal
    settlement_value: Decimal
    underlying_fill: Fill | None


@dataclass(slots=True)
class FixtureGroup:
    """One multi-leg order and the fills the fixture produced for it."""

    order_id: str
    legs: tuple[OrderLeg, ...]
    order_type: OrderType
    limit_price: Decimal | None
    status: str = "working"
    fills: list[Fill] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        """Return whether every leg of the group filled."""
        return self.status == "filled"


class FixtureBook:
    """Minimal deterministic cash and position ledger for one strategy book."""

    __slots__ = (
        "average_costs",
        "book_id",
        "cash",
        "fill_count",
        "initial_cash",
        "positions",
        "realized_pnl",
    )

    def __init__(self, book_id: str, initial_cash: Decimal) -> None:
        self.book_id = book_id
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Decimal] = {}
        self.average_costs: dict[str, Decimal] = {}
        self.fill_count = 0
        self.realized_pnl = _ZERO

    def apply_fill(self, fill: Fill) -> None:
        """Apply one fill to cash, the signed position, and realized PnL."""
        symbol = str(fill.symbol)
        price = fill.price
        quantity = fill.quantity
        if fill.side is Side.BUY:
            self.cash -= price * quantity
        else:
            self.cash += price * quantity
        signed = quantity if fill.side is Side.BUY else -quantity
        current = self.positions.get(symbol, _ZERO)
        average = self.average_costs.get(symbol, _ZERO)
        updated = current + signed
        if current == _ZERO or (current > _ZERO) == (signed > _ZERO):
            basis = average * current + price * signed
            self.average_costs[symbol] = basis / updated
        else:
            reduced = min(abs(signed), abs(current))
            if current > _ZERO:
                self.realized_pnl += (price - average) * reduced
            else:
                self.realized_pnl += (average - price) * reduced
            if abs(signed) > abs(current):
                self.average_costs[symbol] = price
            elif updated == _ZERO:
                self.average_costs.pop(symbol, None)
        self.positions[symbol] = updated
        self.fill_count += 1

    def position(self, symbol: str) -> Decimal:
        """Return the signed position quantity for ``symbol``."""
        return self.positions.get(str(symbol), _ZERO)

    def retire(self, symbol: str) -> None:
        """Zero a settled option position without applying a cash leg."""
        self.positions[str(symbol)] = _ZERO
        self.average_costs.pop(str(symbol), None)

    def book(self, book_id: str) -> FixtureBook:
        """Return this book when the identifier matches."""
        if book_id != self.book_id:
            raise KeyError(f"unknown book {book_id!r}")
        return self


class FixtureUniverse:
    """The canonical symbol universe handed to a strategy."""

    __slots__ = ("symbols",)

    def __init__(self, symbols: Sequence[str]) -> None:
        self.symbols = tuple(str(symbol) for symbol in symbols)

    def __contains__(self, symbol: object) -> bool:
        return str(symbol) in self.symbols

    def __len__(self) -> int:
        return len(self.symbols)


class FixtureLog:
    """Deterministic structured log sink."""

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: list[tuple[str, str, Mapping[str, Any]]] = []

    def _record(self, level: str, message: str, **fields: Any) -> None:
        self.records.append((level, str(message), dict(fields)))

    def debug(self, message: str, **fields: Any) -> None:
        self._record("debug", message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self._record("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._record("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._record("error", message, **fields)


@dataclass(frozen=True, slots=True)
class FixtureEmission:
    """One structured strategy emission."""

    name: str
    fields: Mapping[str, Any]


class FixtureEmissions:
    """Deterministic structured emission sink."""

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: list[FixtureEmission] = []

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None:
        """Record one named emission with structured fields."""
        if not str(name).strip():
            raise ValueError("emission name must be a non-empty string")
        combined: dict[str, Any] = {}
        if fields is not None:
            combined.update(fields)
        combined.update(values)
        self.records.append(FixtureEmission(str(name), combined))

    def named(self, name: str) -> FixtureEmission | None:
        """Return the first emission with ``name``, if any."""
        return next((record for record in self.records if record.name == name), None)

    def fields(self, name: str) -> dict[str, Any]:
        """Merge every field of every emission named ``name``."""
        merged: dict[str, Any] = {}
        for record in self.records:
            if record.name == name:
                merged.update(record.fields)
        return merged


class FixtureData:
    """Point-in-time data capability exposing the option universe snapshot."""

    __slots__ = ("_underlying", "_view")

    def __init__(self, view: OptionUniverseView, underlying: str) -> None:
        self._view = view
        self._underlying = str(Symbol(underlying))

    def read(self, symbol: str | Symbol, *_: object, **__: object) -> object:
        """Return the engine-date snapshot for the configured underlying."""
        if str(Symbol(str(symbol))) != self._underlying:
            return None
        return self._view.resolve()

    def resolve(self) -> OptionUniverseSnapshot:
        """Return the current engine-date snapshot."""
        return self._view.resolve()


class FixtureOrderSink:
    """One-way order boundary backed by the fixture matching kernel."""

    __slots__ = ("_kernel",)

    def __init__(self, kernel: _Kernel) -> None:
        self._kernel = kernel

    def submit(self, order: MultiLegOrder) -> None:
        """Record one atomic multi-leg order for deterministic matching."""
        self._kernel.submit(order)

    def cancel(self, order_id: str) -> None:
        """Cancel one still-working group by identifier."""
        self._kernel.cancel(order_id)

    def owns(self, order_id: str) -> bool:
        """Return whether this strategy submitted ``order_id``."""
        return order_id in self._kernel.groups


class FixtureContext:
    """The constrained strategy context the fixture hands to a strategy."""

    __slots__ = ("_kernel",)

    def __init__(self, kernel: _Kernel) -> None:
        self._kernel = kernel

    @property
    def book_id(self) -> str:
        return self._kernel.book.book_id

    @property
    def clock(self) -> SimClock:
        return self._kernel.clock

    @property
    def universe(self) -> FixtureUniverse:
        return self._kernel.universe

    @property
    def books(self) -> FixtureBook:
        return self._kernel.book

    @property
    def orders(self) -> FixtureOrderSink:
        return self._kernel.orders

    @property
    def data(self) -> FixtureData:
        return self._kernel.data

    @property
    def history(self) -> FixtureData:
        return self._kernel.data

    @property
    def state(self) -> MutableMapping[str, object]:
        return self._kernel.state

    @property
    def random(self) -> Random:
        return self._kernel.random

    @property
    def log(self) -> FixtureLog:
        return self._kernel.log

    @property
    def emissions(self) -> FixtureEmissions:
        return self._kernel.emissions

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None:
        """Emit one structured signal through the fixture sink."""
        self._kernel.emissions.emit(name, fields, **values)


class OptionsMarket:
    """A deterministic in-memory options market configuration.

    ``replay`` builds a fresh kernel, hands the strategy its constrained
    context, and drives the packaged quote stream through the atomic matcher.
    """

    def __init__(
        self,
        *,
        underlying: str,
        contracts: Sequence[ContractRecord],
        front_sets: Sequence[DailyFrontSet],
        quotes: Sequence[FixtureQuote],
        spec: OptionUniverseSpec | None = None,
        book_id: str = "options",
        initial_cash: Decimal = _DEFAULT_CASH,
        seed: int = _DEFAULT_SEED,
        max_dte: int = 14,
    ) -> None:
        self.underlying = str(Symbol(underlying))
        self.contracts = tuple(contracts)
        self.front_sets = tuple(front_sets)
        self.quotes = tuple(
            sorted(
                quotes,
                key=lambda quote: (quote.timestamp, quote.sequence, str(quote.symbol)),
            )
        )
        self.spec = (
            spec
            if spec is not None
            else OptionUniverseSpec(
                underlying=self.underlying,
                mode=SelectionMode.FRONT_SET,
                dte=DteFilter(min_days=0, max_days=max_dte),
            )
        )
        self.book_id = book_id
        self.initial_cash = Decimal(initial_cash)
        self.seed = int(seed)

    def replay[StrategyT: Strategy](self, strategy: StrategyT) -> FixtureRun[StrategyT]:
        """Run ``strategy`` over the packaged quotes and return its artifact."""
        if not self.quotes:
            raise ValueError("the options fixture requires at least one quote")
        clock = SimClock(self.quotes[0].timestamp)
        catalog = ContractsCatalog(
            InMemoryContractRepository(self.contracts),
            InMemoryDailyFrontSetRepository(self.front_sets),
        )
        view = OptionUniverseView(clock, catalog, self.spec)
        book = FixtureBook(self.book_id, self.initial_cash)
        symbols = [self.underlying, *(record.symbol for record in self.contracts)]
        kernel = _Kernel(
            clock=clock,
            data=FixtureData(view, self.underlying),
            book=book,
            universe=FixtureUniverse(symbols),
            seed=self.seed,
        )
        kernel.strategy = strategy
        kernel.context = FixtureContext(kernel)
        context = cast("StrategyContext", kernel.context)
        strategy.on_start(context)
        for quote in self.quotes:
            kernel.deliver(quote)
        strategy.on_stop(context)
        return FixtureRun(strategy, kernel, self)


class _Kernel:
    """The mutable replay kernel behind one :class:`OptionsMarket` run."""

    __slots__ = (
        "book",
        "clock",
        "context",
        "data",
        "emissions",
        "groups",
        "latest",
        "log",
        "orders",
        "random",
        "sequence",
        "state",
        "strategy",
        "universe",
    )

    def __init__(
        self,
        *,
        clock: SimClock,
        data: FixtureData,
        book: FixtureBook,
        universe: FixtureUniverse,
        seed: int,
    ) -> None:
        self.clock = clock
        self.data = data
        self.book = book
        self.universe = universe
        self.orders = FixtureOrderSink(self)
        self.emissions = FixtureEmissions()
        self.log = FixtureLog()
        self.state: MutableMapping[str, object] = {}
        self.random = Random(seed)
        self.latest: dict[Symbol, FixtureQuote] = {}
        self.groups: dict[str, FixtureGroup] = {}
        self.sequence = 0
        self.strategy: Strategy | None = None
        self.context: FixtureContext | None = None

    def deliver(self, quote: FixtureQuote) -> None:
        """Advance the clock, deliver one quote, then match working groups."""
        self.clock.advance_to(quote.timestamp)
        self.latest[quote.symbol] = quote
        strategy = self._strategy()
        strategy.on_quote(quote.event())
        self._match()

    def submit(self, order: MultiLegOrder) -> None:
        """Record one atomic multi-leg order."""
        if not isinstance(order, MultiLegOrder):
            raise TypeError("the options fixture accepts multi-leg orders only")
        self.groups[order.order_id] = FixtureGroup(
            order_id=order.order_id,
            legs=tuple(order.legs),
            order_type=OrderType(order.order_type),
            limit_price=order.limit_price,
        )

    def cancel(self, order_id: str) -> None:
        """Cancel one still-working group."""
        group = self.groups.get(order_id)
        if group is not None and group.status == "working":
            group.status = "canceled"

    def _match(self) -> None:
        for group in tuple(self.groups.values()):
            if group.status == "working":
                self._attempt(group)

    def _attempt(self, group: FixtureGroup) -> None:
        prices: dict[Symbol, Decimal] = {}
        for leg in group.legs:
            quote = self.latest.get(leg.symbol)
            if quote is None:
                return
            prices[leg.symbol] = self._leg_price(leg, quote, group.order_type)
        net = prices[group.legs[0].symbol] - prices[group.legs[1].symbol]
        if group.order_type is OrderType.LIMIT:
            limit = group.limit_price
            if limit is None:
                return
            if group.legs[0].side is Side.SELL:
                if net < limit:
                    return
            elif net > limit:
                return
        fills: list[Fill] = []
        group.status = "filled"
        for leg in group.legs:
            price = prices[leg.symbol]
            self.sequence += 1
            fill = Fill(
                event_id=f"{group.order_id}:{leg.leg_id or str(leg.symbol)}",
                timestamp=self.clock.now(),
                sequence=self.sequence,
                fill_id=f"fill-{self.sequence:04d}",
                order_id=group.order_id,
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
                price=price,
                liquidity=("maker" if group.order_type is OrderType.LIMIT else "taker"),
            )
            group.fills.append(fill)
            fills.append(fill)
        for fill in fills:
            self.book.apply_fill(fill)
        strategy = self._strategy()
        for fill in fills:
            strategy.on_fill(fill)

    @staticmethod
    def _leg_price(
        leg: OrderLeg, quote: FixtureQuote, order_type: OrderType
    ) -> Decimal:
        if order_type is OrderType.MARKET:
            return quote.ask_price if leg.side is Side.BUY else quote.bid_price
        return quote.mid

    def _strategy(self) -> Strategy:
        if self.strategy is None:
            raise RuntimeError("the fixture kernel started before a strategy")
        return self.strategy


class FixtureRun[StrategyT: Strategy]:
    """One completed fixture replay and its deterministic artifacts."""

    __slots__ = ("_kernel", "_market", "strategy")

    def __init__(
        self, strategy: StrategyT, kernel: _Kernel, market: OptionsMarket
    ) -> None:
        self.strategy = strategy
        self._kernel = kernel
        self._market = market

    @property
    def clock(self) -> SimClock:
        """Return the replay clock."""
        return self._kernel.clock

    @property
    def book(self) -> FixtureBook:
        """Return the strategy book ledger."""
        return self._kernel.book

    @property
    def emissions(self) -> FixtureEmissions:
        """Return the structured emission sink."""
        return self._kernel.emissions

    @property
    def data(self) -> FixtureData:
        """Return the point-in-time data capability."""
        return self._kernel.data

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Return every fill in deterministic match order."""
        return tuple(
            fill for group in self._kernel.groups.values() for fill in group.fills
        )

    @property
    def groups(self) -> tuple[FixtureGroup, ...]:
        """Return every recorded order group in submission order."""
        return tuple(self._kernel.groups.values())

    def order(self, order_id: str) -> FixtureGroup:
        """Return one recorded group by identifier."""
        return self._kernel.groups[order_id]

    def settle(
        self, spot: Decimal | str, *, timestamp: datetime | None = None
    ) -> tuple[FixtureSettlement, ...]:
        """Settle open option positions at expiration against ``spot``.

        Long in-the-money contracts exercise into an exact underlying fill at
        the strike; short in-the-money contracts are assigned into the opposite
        underlying fill; everything else expires worthless.  Option positions
        are retired after settlement so the book's remaining risk is the
        underlying position the settlement moved.
        """
        price = Decimal(str(spot))
        if price <= _ZERO:
            raise ValueError("spot must be positive")
        now = self._kernel.clock.now() if timestamp is None else timestamp
        settlements: list[FixtureSettlement] = []
        book = self._kernel.book
        for contract in sorted(
            self._market.contracts, key=lambda record: record.symbol
        ):
            quantity = book.position(contract.symbol)
            if quantity == _ZERO:
                continue
            intrinsic = _intrinsic(contract, price)
            fill: Fill | None = None
            settlement_value = _ZERO
            if quantity > _ZERO:
                if intrinsic > _ZERO:
                    side = (
                        Side.SELL
                        if contract.option_type is OptionType.PUT
                        else Side.BUY
                    )
                    fill = self._underlying_fill(contract, side, quantity, price, now)
                    book.apply_fill(fill)
                    kind = SettlementKind.EXERCISED
                    contracts = quantity
                else:
                    kind = SettlementKind.WORTHLESS
                    contracts = _ZERO
            else:
                short_quantity = -quantity
                if intrinsic > _ZERO:
                    side = (
                        Side.SELL
                        if contract.option_type is OptionType.CALL
                        else Side.BUY
                    )
                    fill = self._underlying_fill(
                        contract, side, short_quantity, price, now
                    )
                    book.apply_fill(fill)
                    kind = SettlementKind.ASSIGNED
                    contracts = short_quantity
                else:
                    kind = SettlementKind.WORTHLESS
                    contracts = _ZERO
            book.retire(contract.symbol)
            settlements.append(
                FixtureSettlement(
                    symbol=contract.symbol,
                    kind=kind,
                    contracts=contracts,
                    spot=price,
                    settlement_value=settlement_value,
                    underlying_fill=fill,
                )
            )
        return tuple(settlements)

    def _underlying_fill(
        self,
        contract: ContractRecord,
        side: Side,
        contracts: Decimal,
        price: Decimal,
        timestamp: datetime,
    ) -> Fill:
        self._kernel.sequence += 1
        sequence = self._kernel.sequence
        return Fill(
            event_id=f"settle:{contract.symbol}:{sequence}",
            timestamp=timestamp,
            sequence=sequence,
            fill_id=f"settle-{sequence:04d}",
            order_id=f"settle:{contract.symbol}",
            symbol=Symbol(self._market.underlying),
            side=side,
            quantity=contracts * Decimal(contract.multiplier),
            price=contract.strike_price,
            liquidity="settlement",
        )


def _intrinsic(contract: ContractRecord, spot: Decimal) -> Decimal:
    if contract.option_type is OptionType.CALL:
        return max(spot - contract.strike_price, _ZERO)
    return max(contract.strike_price - spot, _ZERO)


def sps_contract(
    symbol: str, strike: Decimal | str, *, expiry: date = SPS_EXPIRY
) -> ContractRecord:
    """Build one SPY put contract for the canonical fixture."""
    return ContractRecord(
        symbol=symbol,
        underlying=SPS_UNDERLYING,
        expiration_date=expiry,
        strike_price=Decimal(str(strike)),
        option_type=OptionType.PUT,
        multiplier=SPS_MULTIPLIER,
        listed_date=date(2024, 1, 2),
    )


def sps_quote(
    symbol: str,
    day: date,
    sequence: int,
    *,
    bid: str | Decimal,
    ask: str | Decimal,
    offset_us: int = 0,
) -> FixtureQuote:
    """Build one per-contract notional quote from per-share bid/ask inputs."""
    timestamp = datetime(
        day.year, day.month, day.day, _SESSION_HOUR, _SESSION_MINUTE, tzinfo=UTC
    ) + timedelta(microseconds=offset_us)
    scale = Decimal(SPS_MULTIPLIER)
    return FixtureQuote(
        symbol=Symbol(symbol),
        timestamp=timestamp,
        sequence=sequence,
        bid_price=Decimal(str(bid)) * scale,
        ask_price=Decimal(str(ask)) * scale,
    )


def sps_round(
    day: date,
    sequence: int,
    *,
    short: tuple[str, str],
    long: tuple[str, str],
    offset_us: int = 0,
) -> tuple[FixtureQuote, FixtureQuote]:
    """Build one short-then-long quote round for the spread legs."""
    return (
        sps_quote(
            SPS_SHORT_OSI,
            day,
            sequence,
            bid=short[0],
            ask=short[1],
            offset_us=offset_us,
        ),
        sps_quote(
            SPS_LONG_OSI,
            day,
            sequence + 1,
            bid=long[0],
            ask=long[1],
            offset_us=offset_us + 2,
        ),
    )


def sps_catalog() -> ContractsCatalog:
    """Return a catalog whose daily front sets expose only the spread legs."""
    short = sps_contract(SPS_SHORT_OSI, SPS_SHORT_STRIKE)
    long = sps_contract(SPS_LONG_OSI, SPS_LONG_STRIKE)
    feb = sps_contract(SPS_FEB_OSI, SPS_SHORT_STRIKE, expiry=date(2024, 2, 16))
    front_sets = tuple(
        DailyFrontSet(
            underlying=SPS_UNDERLYING,
            selection_date=day,
            contracts=(short, long),
        )
        for day in (SPS_ENTRY_DAY, SPS_MANAGED_DAY, SPS_EXPIRY)
    )
    return ContractsCatalog(
        InMemoryContractRepository((short, long, feb)),
        InMemoryDailyFrontSetRepository(front_sets),
    )


def sps_market(
    quotes: Iterable[FixtureQuote],
    *,
    book_id: str = "short-put-spread",
    initial_cash: Decimal = _DEFAULT_CASH,
    max_dte: int = 14,
) -> OptionsMarket:
    """Build the canonical short-put-spread market over ``quotes``."""
    catalog = sps_catalog()
    contracts = tuple(catalog.list(underlying=SPS_UNDERLYING))
    front_sets = catalog.list_front_sets(underlying=SPS_UNDERLYING)
    return OptionsMarket(
        underlying=SPS_UNDERLYING,
        contracts=contracts,
        front_sets=front_sets,
        quotes=tuple(quotes),
        spec=OptionUniverseSpec(
            underlying=SPS_UNDERLYING,
            mode=SelectionMode.FRONT_SET,
            dte=DteFilter(min_days=0, max_days=max_dte),
        ),
        book_id=book_id,
        initial_cash=initial_cash,
    )


__all__ = [
    "FixtureBook",
    "FixtureContext",
    "FixtureData",
    "FixtureEmission",
    "FixtureEmissions",
    "FixtureGroup",
    "FixtureQuote",
    "FixtureRun",
    "FixtureSettlement",
    "OptionsMarket",
    "SPS_ENTRY_DAY",
    "SPS_EXPIRY",
    "SPS_FEB_OSI",
    "SPS_LONG_OSI",
    "SPS_LONG_STRIKE",
    "SPS_MANAGED_DAY",
    "SPS_MULTIPLIER",
    "SPS_SHORT_OSI",
    "SPS_SHORT_STRIKE",
    "SPS_UNDERLYING",
    "SettlementKind",
    "sps_catalog",
    "sps_contract",
    "sps_market",
    "sps_quote",
    "sps_round",
]
