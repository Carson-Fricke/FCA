"""Constrained, mode-independent dependencies made available to strategies."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from decimal import Decimal
from random import Random
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from harness.core.clock import Clock, ReadonlyClock
from harness.core.events import EventBus, EventSelector
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types import (
    AccountSnapshot,
    Book,
    Event,
    EventType,
    ExecutionReport,
    Fill,
    Position,
    QuoteEvent,
    TradeEvent,
)
from harness.core.types.orders import OrderRequest
from harness.data.history import HistoricalView, StrategyHistory
from harness.data.listing_events import CorporateActionEvent
from harness.portfolio import (
    BookSnapshot,
    DuplicateFillError,
    PortfolioHub,
    PortfolioSnapshot,
    PositionSnapshot,
    UnknownBookError,
)
from harness.venues import ExecutionVenue


@runtime_checkable
class PointInTimeData(Protocol):
    """Read-only, now-anchored point-in-time data boundary.

    The strategy contract deliberately does not prescribe a storage engine.
    Every read is stamped to the engine clock head by the supplying shell: the
    boundary takes no timestamp parameter, so strategy code cannot express a
    query for data beyond the engine clock, and returned values are immutable
    snapshots.  Later history adapters can implement ``read`` while replay and
    live shells provide equivalent views.
    """

    def read(
        self,
        symbol: str | Symbol,
        data_type: type[Event] | EventType | None = None,
    ) -> object:
        """Read the newest matching data at or before the engine clock head."""
        ...


@runtime_checkable
class Scheduler(Protocol):
    """Boundary for deterministic event scheduling."""

    def schedule(self, event: Event) -> None:
        """Schedule an already timestamped event."""
        ...


@runtime_checkable
class StructuredLogger(Protocol):
    """Structural interface for a strategy's structured log sink."""

    def debug(self, message: str, **fields: object) -> None: ...

    def info(self, message: str, **fields: object) -> None: ...

    def warning(self, message: str, **fields: object) -> None: ...

    def error(self, message: str, **fields: object) -> None: ...


@runtime_checkable
class Emitter(Protocol):
    """Structural interface for a strategy's structured emission sink."""

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None: ...


_RESERVED_EVENT_CLASSES: Final[tuple[type[Event], ...]] = (
    QuoteEvent,
    TradeEvent,
    ExecutionReport,
    Fill,
    Position,
    Book,
    AccountSnapshot,
)


class EventScheduler:
    """Adapt the existing event bus to the strategy scheduler boundary.

    Market data and venue or portfolio outcomes are reserved for the feed and
    the venue. The capability rejects those event classes before they reach
    the event bus, so fabricated market data cannot originate from
    ``context.schedule`` in any shell. The reserved set mirrors the engine
    scheduler and is duplicated here because ``harness.strategy`` must not
    import ``harness.runtime``.
    """

    __slots__ = ("_bus",)

    def __init__(self, bus: EventBus) -> None:
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        self._bus = bus

    def schedule(self, event: Event) -> None:
        """Schedule one strategy-owned event; reserved classes are refused."""
        if isinstance(event, _RESERVED_EVENT_CLASSES):
            raise TypeError(
                f"{type(event).__name__} events are reserved for the feed and "
                "the venue; strategies cannot schedule them"
            )
        self._bus.schedule(event)


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One structured strategy log record."""

    level: str
    message: str
    fields: Mapping[str, object]


class StructuredLog:
    """In-memory structured logger suitable for deterministic tests."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[LogRecord] = []

    @property
    def records(self) -> tuple[LogRecord, ...]:
        """Return records in the order emitted by the strategy."""
        return tuple(self._records)

    def debug(self, message: str, **fields: object) -> None:
        """Record a debug message and its structured fields."""
        self._record("debug", message, fields)

    def info(self, message: str, **fields: object) -> None:
        """Record an informational message and its structured fields."""
        self._record("info", message, fields)

    def warning(self, message: str, **fields: object) -> None:
        """Record a warning and its structured fields."""
        self._record("warning", message, fields)

    def error(self, message: str, **fields: object) -> None:
        """Record an error and its structured fields."""
        self._record("error", message, fields)

    def _record(self, level: str, message: str, fields: Mapping[str, object]) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("log message must be a non-empty string")
        self._records.append(LogRecord(level, message, MappingProxyType(dict(fields))))


@dataclass(frozen=True, slots=True)
class Emission:
    """One structured strategy emission."""

    name: str
    fields: Mapping[str, object]


class EmissionSink:
    """Deterministic in-memory sink for strategy metrics and signals."""

    __slots__ = ("_emissions",)

    def __init__(self) -> None:
        self._emissions: list[Emission] = []

    @property
    def records(self) -> tuple[Emission, ...]:
        """Return emissions in insertion order."""
        return tuple(self._emissions)

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None:
        """Store one named emission with immutable structured fields."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("emission name must be a non-empty string")
        combined = {} if fields is None else dict(fields)
        combined.update(values)
        self._emissions.append(Emission(name, MappingProxyType(combined)))


def _freeze_state(value: object, path: str) -> object:
    """Freeze configuration-shaped state while rejecting binary floats."""
    if value is None or isinstance(value, str | bool | int | Decimal):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating point")
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                _state_key(key, f"{path}.key"): _freeze_state(item, f"{path}.{key}")
                for key, item in value.items()
            }
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_state(item, f"{path}[]") for item in value)
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _state_key(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{path} must be a non-empty string")
    return value.strip()


class StateStore(MutableMapping[str, object]):
    """Small deterministic mutable state store with immutable nested values."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        self._values: dict[str, object] = {}
        if values is not None:
            for key, value in values.items():
                self[key] = value

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __setitem__(self, key: str, value: object) -> None:
        normalized = _state_key(key, "state key")
        self._values[normalized] = _freeze_state(value, f"state[{normalized!r}]")

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    @property
    def snapshot(self) -> Mapping[str, object]:
        """Return a shallow immutable view of the current state."""
        return MappingProxyType(dict(self._values))


class UniverseView:
    """Immutable canonical symbol universe available to one strategy."""

    __slots__ = ("_symbols",)

    def __init__(self, symbols: Iterable[str | Symbol] = ()) -> None:
        if isinstance(symbols, (str, Symbol)):
            symbols = (symbols,)
        normalized = tuple(sorted({canonical_symbol(symbol) for symbol in symbols}))
        self._symbols = normalized

    @property
    def symbols(self) -> tuple[Symbol, ...]:
        """Return symbols in stable lexical order."""
        return self._symbols

    def __contains__(self, symbol: object) -> bool:
        if not isinstance(symbol, str):
            return False
        try:
            return canonical_symbol(symbol) in self._symbols
        except (TypeError, ValueError):
            return False

    def __iter__(self) -> Iterator[Symbol]:
        return iter(self._symbols)

    def __len__(self) -> int:
        return len(self._symbols)

    def contains(self, symbol: str | Symbol) -> bool:
        """Return whether a symbol belongs to this universe."""
        return symbol in self


@dataclass(frozen=True, slots=True)
class Subscription:
    """One event selector optionally limited to canonical symbols."""

    selector: EventSelector
    symbols: tuple[Symbol, ...] | None = None

    def __post_init__(self) -> None:
        selector = self.selector
        if not isinstance(selector, (type, EventType)):
            raise TypeError("selector must be an Event class or EventType")
        if isinstance(selector, type) and not issubclass(selector, Event):
            raise TypeError("selector must be an Event class or EventType")
        if self.symbols is not None:
            if isinstance(self.symbols, (str, Symbol)):
                symbols = (self.symbols,)
            else:
                symbols = self.symbols
            normalized = tuple(sorted({canonical_symbol(symbol) for symbol in symbols}))
            if not normalized:
                raise ValueError("subscription symbols must not be empty")
            object.__setattr__(self, "symbols", normalized)

    def matches(self, event: Event) -> bool:
        """Return whether this selector accepts one event."""
        if isinstance(self.selector, EventType):
            if event.event_type is not self.selector:
                return False
        elif not isinstance(event, self.selector):
            return False
        if self.symbols is None:
            return True
        event_symbols = _event_symbols(event)
        return not event_symbols or bool(event_symbols.intersection(self.symbols))


class SubscriptionSet:
    """Mutable subscription boundary with deterministic filtering semantics."""

    __slots__ = ("_subscriptions", "_universe")

    def __init__(
        self,
        universe: UniverseView,
        subscriptions: Iterable[Subscription | EventSelector] | None = None,
    ) -> None:
        self._universe = universe
        self._subscriptions = [
            subscription
            if isinstance(subscription, Subscription)
            else Subscription(subscription)
            for subscription in subscriptions or ()
        ]
        for subscription in self._subscriptions:
            self._validate_symbols(subscription.symbols)

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        """Return explicit subscriptions in registration order."""
        return tuple(self._subscriptions)

    def subscribe(
        self,
        selector: EventSelector,
        symbols: Iterable[str | Symbol] | str | Symbol | None = None,
    ) -> Subscription:
        """Subscribe to an event class/type and optional universe symbols."""
        normalized = _normalize_subscription_symbols(symbols)
        self._validate_symbols(normalized)
        subscription = Subscription(selector, normalized)
        if subscription not in self._subscriptions:
            self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(
        self,
        selector: EventSelector,
        symbols: Iterable[str | Symbol] | str | Symbol | None = None,
    ) -> None:
        """Remove matching subscriptions; missing entries are harmless."""
        normalized = _normalize_subscription_symbols(symbols)
        self._validate_symbols(normalized)
        candidate = Subscription(selector, normalized)
        self._subscriptions = [
            subscription
            for subscription in self._subscriptions
            if subscription != candidate
        ]

    def is_subscribed(self, event: Event) -> bool:
        """Return whether an event is admitted by this boundary."""
        symbols = _event_symbols(event)
        if (
            self._universe.symbols
            and symbols
            and not symbols.intersection(self._universe.symbols)
        ):
            return False
        return any(subscription.matches(event) for subscription in self._subscriptions)

    def _validate_symbols(self, symbols: tuple[Symbol, ...] | None) -> None:
        if symbols is not None and self._universe.symbols:
            unknown = set(symbols).difference(self._universe.symbols)
            if unknown:
                raise ValueError(
                    "subscription symbols must belong to the strategy universe"
                )


def _normalize_subscription_symbols(
    symbols: Iterable[str | Symbol] | str | Symbol | None,
) -> tuple[Symbol, ...] | None:
    if symbols is None:
        return None
    if isinstance(symbols, (str, Symbol)):
        symbols = (symbols,)
    normalized = tuple(sorted({canonical_symbol(symbol) for symbol in symbols}))
    if not normalized:
        raise ValueError("subscription symbols must not be empty")
    return normalized


def _event_symbols(event: Event) -> frozenset[Symbol]:
    symbol = getattr(event, "symbol", None)
    if symbol is not None:
        return frozenset({canonical_symbol(symbol)})
    legs = getattr(event, "legs", ())
    return frozenset(
        canonical_symbol(leg.symbol) for leg in legs if hasattr(leg, "symbol")
    )


def _scoped_snapshot(snapshot: PortfolioSnapshot, book_id: str) -> PortfolioSnapshot:
    """Return the shared-account aggregate scoped to one strategy book.

    Aggregate account totals (cash, positions, fill count) are the account
    the strategy trades in and stay observable. Sibling virtual-book
    constituents are removed, so another strategy's book is not representable
    through the returned value.
    """
    return PortfolioSnapshot(
        account_id=snapshot.account_id,
        initial_cash=snapshot.initial_cash,
        cash=snapshot.cash,
        positions=snapshot.positions,
        books=(snapshot.book(book_id),),
        fill_count=snapshot.fill_count,
    )


class BooksView:
    """Read-only strategy view over a hub and one strategy book.

    The view resolves only the context's own book; sibling virtual books in
    the shared account are not observable. Aggregate shared-account totals
    remain visible as the explicitly named account capability.
    """

    __slots__ = ("_book_id", "_hub")

    def __init__(self, hub: PortfolioHub, book_id: str) -> None:
        self._hub = hub
        self._book_id = book_id

    @property
    def book_id(self) -> str:
        """Return the strategy's virtual book identifier."""
        return self._book_id

    @property
    def snapshot(self) -> PortfolioSnapshot:
        """Return the shared-account aggregate scoped to this strategy book."""
        return _scoped_snapshot(self._hub.snapshot, self._book_id)

    @property
    def current(self) -> BookSnapshot:
        """Return the current immutable strategy book snapshot."""
        return self.snapshot.book(self._book_id)

    @property
    def cash(self) -> Decimal:
        """Return current strategy cash."""
        return self.current.cash

    @property
    def equity(self) -> Decimal | None:
        """Return marked strategy equity."""
        return self.current.equity

    @property
    def fill_count(self) -> int:
        """Return the number of fills accounted for this strategy book."""
        return self.current.fill_count

    @property
    def book_ids(self) -> tuple[str, ...]:
        """Return this strategy's own virtual book identifier."""
        return (self._book_id,)

    def book(self, book_id: str) -> BookSnapshot:
        """Return the strategy's own immutable book snapshot."""
        if book_id != self._book_id:
            raise UnknownBookError(book_id)
        return self.snapshot.book(book_id)

    def position(self, symbol: str | Symbol) -> PositionSnapshot:
        """Return one current position from this strategy book."""
        return self.current.position(symbol)


class AccountView:
    """Read-only account view combining portfolio and latest account event.

    The fallback portfolio snapshot is scoped to one strategy book, so
    account-level totals are observable while sibling virtual books are not.
    """

    __slots__ = ("_account_event", "_book_id", "_hub")

    def __init__(self, hub: PortfolioHub, book_id: str | None = None) -> None:
        self._hub = hub
        self._book_id = book_id
        self._account_event: AccountSnapshot | None = None

    @property
    def account_id(self) -> str:
        """Return the configured shared account identifier."""
        return self._hub.account_id

    @property
    def snapshot(self) -> AccountSnapshot | PortfolioSnapshot:
        """Return the latest account event or the book-scoped aggregate."""
        if self._account_event is not None:
            return self._account_event
        if self._book_id is None:
            return self._hub.snapshot
        return _scoped_snapshot(self._hub.snapshot, self._book_id)

    @property
    def cash(self) -> Decimal:
        """Return latest account cash, falling back to the portfolio aggregate."""
        return self.snapshot.cash

    @property
    def equity(self) -> Decimal | None:
        """Return current account equity when the source supplies a mark."""
        return self.snapshot.equity

    @property
    def buying_power(self) -> Decimal | None:
        """Return broker buying power when an account event has supplied it."""
        return None if self._account_event is None else self._account_event.buying_power

    def update(self, event: AccountSnapshot) -> None:
        """Update the view from a validated account event."""
        if event.account_id == self.account_id:
            self._account_event = event


class OrdersView:
    """Fire-and-forget order boundary that records this strategy's ownership."""

    __slots__ = ("_owned_order_ids", "_owned_order_id_set", "_venue")

    def __init__(self, venue: ExecutionVenue | None) -> None:
        if venue is not None and not isinstance(venue, ExecutionVenue):
            raise TypeError("venue must implement ExecutionVenue")
        self._venue = venue
        self._owned_order_ids: list[str] = []
        self._owned_order_id_set: set[str] = set()

    @property
    def submitted_order_ids(self) -> tuple[str, ...]:
        """Return order identifiers submitted by this strategy."""
        return tuple(self._owned_order_ids)

    @property
    def venue(self) -> ExecutionVenue | None:
        """Return the injected venue without exposing mutable adapter state."""
        return self._venue

    def owns(self, order_id: str) -> bool:
        """Return whether a fill's order belongs to this strategy."""
        return order_id in self._owned_order_id_set

    def submit(self, order: OrderRequest) -> None:
        """Submit an order without synchronously fabricating its outcome."""
        self._require_venue().submit(order)
        self._own(order.order_id)

    def cancel(self, order_id: str) -> None:
        """Request cancellation through the injected venue."""
        self._require_venue().cancel(order_id)

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        """Request replacement through the injected venue."""
        self._require_venue().replace(order_id, replacement)
        self._own(replacement.order_id)

    def request_position_snapshot(self, account_id: str) -> None:
        """Request an account snapshot through the injected venue."""
        self._require_venue().request_position_snapshot(account_id)

    def _require_venue(self) -> ExecutionVenue:
        if self._venue is None:
            raise RuntimeError("strategy context has no execution venue")
        return self._venue

    def _own(self, order_id: str) -> None:
        if order_id not in self._owned_order_id_set:
            self._owned_order_ids.append(order_id)
            self._owned_order_id_set.add(order_id)


class StrategyContext:
    """Mode-independent, constrained dependencies for one strategy instance.

    Callback order is ``on_start``, quote/trade/order/report/fill/position/
    book/account callbacks in the event-bus order, then engine-delivered
    corporate actions (``on_corporate_action``) on their knowledge dates, and
    finally ``on_stop``.  The context never creates a clock, reads wall time,
    opens a network connection, or uses process-global randomness.
    """

    __slots__ = (
        "_account",
        "_book_id",
        "_bus",
        "_clock",
        "_corporate_actions",
        "_data",
        "_emissions",
        "_log",
        "_orders",
        "_portfolio",
        "_random",
        "_scheduler",
        "_state",
        "_subscriptions",
        "_universe",
        "_books",
    )

    def __init__(
        self,
        clock: Clock,
        *,
        portfolio: PortfolioHub | None = None,
        books: PortfolioHub | None = None,
        venue: ExecutionVenue | None = None,
        bus: EventBus | None = None,
        data: HistoricalView | PointInTimeData | None = None,
        scheduler: Scheduler | None = None,
        state: Mapping[str, object] | None = None,
        seed: int = 0,
        random_seed: int | None = None,
        universe: Iterable[str | Symbol] = (),
        subscriptions: Iterable[Subscription | EventSelector] | None = None,
        book_id: str | None = None,
        logger: StructuredLogger | None = None,
        emissions: Emitter | None = None,
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if portfolio is not None and books is not None and portfolio is not books:
            raise ValueError("portfolio and books must refer to the same hub")
        hub = portfolio if portfolio is not None else books
        if hub is None or not isinstance(hub, PortfolioHub):
            raise TypeError("portfolio must be a PortfolioHub")
        if not hub.book_ids:
            raise ValueError("portfolio must contain at least one strategy book")
        selected_book = book_id
        if selected_book is None:
            if len(hub.book_ids) != 1:
                raise ValueError(
                    "book_id is required when a portfolio has multiple books"
                )
            selected_book = hub.book_ids[0]
        assert selected_book is not None
        hub.book(selected_book)
        if bus is None and scheduler is None:
            raise ValueError("bus or scheduler is required")
        if bus is not None and not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        if scheduler is not None and not isinstance(scheduler, Scheduler):
            raise TypeError("scheduler must implement Scheduler")
        if data is not None and not isinstance(data, PointInTimeData | HistoricalView):
            raise TypeError("data must implement PointInTimeData")
        if logger is not None and not isinstance(logger, StructuredLogger):
            raise TypeError("logger must implement StructuredLogger")
        if emissions is not None and not isinstance(emissions, Emitter):
            raise TypeError("emissions must implement Emitter")
        actual_seed = seed if random_seed is None else random_seed
        if isinstance(actual_seed, bool) or not isinstance(actual_seed, int):
            raise TypeError("seed must be a non-negative integer")
        if actual_seed < 0:
            raise ValueError("seed must be a non-negative integer")

        self._clock = (
            clock if isinstance(clock, ReadonlyClock) else ReadonlyClock(clock)
        )
        self._bus = bus
        self._corporate_actions: tuple[CorporateActionEvent, ...] = ()
        self._portfolio = hub
        self._book_id = selected_book
        if data is None:
            self._data = None
        elif isinstance(data, StrategyHistory):
            self._data = data
        elif isinstance(data, HistoricalView):
            self._data = StrategyHistory(data, clock)
        else:
            self._data = data
        if scheduler is None:
            assert bus is not None
            scheduler = EventScheduler(bus)
        self._scheduler = scheduler
        self._state = state if isinstance(state, StateStore) else StateStore(state)
        self._random = Random(actual_seed)
        self._universe = UniverseView(universe)
        self._subscriptions = SubscriptionSet(self._universe, subscriptions)
        self._account = AccountView(hub, selected_book)
        self._orders = OrdersView(venue)
        self._log = StructuredLog() if logger is None else logger
        self._emissions = EmissionSink() if emissions is None else emissions
        self._books = BooksView(hub, selected_book)

    @property
    def clock(self) -> ReadonlyClock:
        """Return the read-only clock capability for observed engine time."""
        return self._clock

    @property
    def account(self) -> AccountView:
        """Return the constrained account view."""
        return self._account

    @property
    def books(self) -> BooksView:
        """Return read-only strategy book and portfolio views."""
        return self._books

    @property
    def portfolio(self) -> PortfolioSnapshot:
        """Return the immutable shared portfolio snapshot."""
        return self._books.snapshot

    @property
    def orders(self) -> OrdersView:
        """Return the constrained order and venue command boundary."""
        return self._orders

    @property
    def venue(self) -> ExecutionVenue | None:
        """Return the injected venue boundary, if one is configured."""
        return self._orders.venue

    @property
    def data(self) -> StrategyHistory | PointInTimeData | None:
        """Return the opaque, now-anchored point-in-time data capability.

        Engine views are wrapped into :class:`StrategyHistory` at this
        boundary, so the strategy-reachable surface takes no timestamps and
        exposes no record, subscribe, attach, or clock handles.
        """
        return self._data

    @property
    def history(self) -> StrategyHistory | PointInTimeData | None:
        """Alias for the point-in-time data capability."""
        return self._data

    @property
    def scheduler(self) -> Scheduler:
        """Return the deterministic scheduler boundary."""
        return self._scheduler

    @property
    def state(self) -> StateStore:
        """Return mutable strategy-local deterministic state."""
        return self._state

    @property
    def random(self) -> Random:
        """Return the per-context seeded random source."""
        return self._random

    @property
    def rng(self) -> Random:
        """Alias for the seeded random source."""
        return self._random

    @property
    def universe(self) -> UniverseView:
        """Return the immutable strategy universe."""
        return self._universe

    @property
    def subscriptions(self) -> SubscriptionSet:
        """Return the dynamic event subscription boundary."""
        return self._subscriptions

    @property
    def log(self) -> StructuredLogger:
        """Return the structured logging sink."""
        return self._log

    @property
    def logger(self) -> StructuredLogger:
        """Alias for the structured logging sink."""
        return self._log

    @property
    def emissions(self) -> Emitter:
        """Return the structured emission sink."""
        return self._emissions

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None:
        """Emit one structured signal through the injected sink."""
        self._emissions.emit(name, fields, **values)

    def schedule(self, event: Event) -> None:
        """Schedule one explicitly timestamped event."""
        self._scheduler.schedule(event)

    def subscribe(
        self,
        selector: EventSelector,
        symbols: Iterable[str | Symbol] | str | Symbol | None = None,
    ) -> Subscription:
        """Convenience method for dynamic subscriptions."""
        return self._subscriptions.subscribe(selector, symbols)

    def unsubscribe(
        self,
        selector: EventSelector,
        symbols: Iterable[str | Symbol] | str | Symbol | None = None,
    ) -> None:
        """Convenience method for removing dynamic subscriptions."""
        self._subscriptions.unsubscribe(selector, symbols)

    def _bus_for_binding(self) -> EventBus | None:
        """Return the bus to the binding adapter without widening the API."""
        return self._bus

    @property
    def book_id(self) -> str:
        """Return this context's virtual book identifier."""
        return self._book_id

    def _accepts(self, event: Event) -> bool:
        return self._subscriptions.is_subscribed(event)

    def _account_fill(self, fill: Fill, accounted_fill_ids: set[str]) -> None:
        if fill.fill_id in accounted_fill_ids:
            raise DuplicateFillError(f"fill {fill.fill_id!r} was delivered twice")
        if not self._orders.owns(fill.order_id) and len(self._portfolio.book_ids) != 1:
            return
        try:
            self._portfolio.apply_fill(self._book_id, fill)
        except DuplicateFillError:
            # A shared bus may deliver one globally routed fill to another
            # strategy binding. Only the owning binding mutates its book.
            return
        accounted_fill_ids.add(fill.fill_id)

    def _account_event(self, event: AccountSnapshot) -> None:
        self._account.update(event)

    def _deliver_corporate_action(self, event: CorporateActionEvent) -> bool:
        """Record one engine-delivered lifecycle event on the context surface.

        The strategy binding calls this immediately before invoking the
        strategy's ``on_corporate_action`` hook with ``(event, self)``.  The
        context-side record makes delivery idempotent -- a repeated event is
        refused and never re-recorded -- so strategies and artifacts observe
        each lifecycle event exactly once.  Returns whether the event was
        newly delivered.
        """
        if not isinstance(event, CorporateActionEvent):
            raise TypeError("event must be a CorporateActionEvent")
        if event in self._corporate_actions:
            return False
        self._corporate_actions = (*self._corporate_actions, event)
        return True

    @property
    def corporate_actions(self) -> tuple[CorporateActionEvent, ...]:
        """Return engine-delivered lifecycle events in delivery order."""
        return self._corporate_actions


__all__ = [
    "AccountView",
    "BooksView",
    "Emission",
    "EmissionSink",
    "Emitter",
    "EventScheduler",
    "LogRecord",
    "OrdersView",
    "PointInTimeData",
    "ReadonlyClock",
    "Scheduler",
    "StateStore",
    "StrategyHistory",
    "StructuredLog",
    "StructuredLogger",
    "Subscription",
    "SubscriptionSet",
    "StrategyContext",
    "UniverseView",
]
