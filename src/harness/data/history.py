"""Point-in-time historical market-data view with hard look-ahead clipping.

The view retains quote and trade events in bounded per-subscription ring
buffers and answers point-in-time queries relative to an injected engine clock.
Every query is clipped to the clock head: asking for data newer than the head
raises :class:`LookaheadError` instead of silently leaking the future.  No code
path here reads wall time, consumes randomness, touches the network, or
performs I/O.

Timestamps are compared on an integer nanosecond timeline so sub-microsecond
engine boundaries survive the query path even though ``datetime`` itself only
carries microsecond resolution.  Callers may therefore pass either a
timezone-aware ``datetime`` or an integer number of nanoseconds since the Unix
epoch.
"""

from __future__ import annotations

import sys
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, cast

from harness.core.clock import Clock
from harness.core.events import (
    EVENT_CLASS_PRIORITY,
    EventBus,
    EventSelector,
)
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import timestamp
from harness.core.types.events import Event, EventType, QuoteEvent, TradeEvent
from harness.data.bars import BarAggregator, OHLCVBar, interval_delta

Nanos = int
"""Integer nanoseconds since the Unix epoch."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_SECOND: Final = 1_000_000_000
_NS_PER_MICROSECOND: Final = 1_000
_ZERO: Final = Decimal("0")
_MAX_TAIL: Final = sys.maxsize

DEFAULT_HISTORY_CAPACITY: Final = 4096
_HISTORY_KINDS: Final = (EventType.QUOTE, EventType.TRADE)


class LookaheadError(RuntimeError):
    """Raised when a caller asks for data newer than the engine clock head."""


def to_nanos(value: datetime | int, field_name: str = "timestamp") -> Nanos:
    """Normalize a timestamp to integer nanoseconds since the Unix epoch.

    ``datetime`` values are converted at their native microsecond resolution.
    Integers are treated as nanoseconds already and pass through unchanged so
    callers can address engine boundaries finer than one microsecond.
    """
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a datetime or nanosecond integer")
    if isinstance(value, int):
        return value
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime or nanosecond integer")
    stamp = timestamp(value, field_name)
    delta = stamp - _EPOCH
    return (
        delta.days * 86_400 + delta.seconds
    ) * _NS_PER_SECOND + delta.microseconds * _NS_PER_MICROSECOND


def from_nanos(value: Nanos) -> datetime:
    """Return the UTC datetime at or before an integer nanosecond instant."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("value must be an integer number of nanoseconds")
    microseconds, _ = divmod(value, _NS_PER_MICROSECOND)
    return _EPOCH + timedelta(microseconds=microseconds)


def _ordering_key(event: Event) -> tuple[Nanos, int, int]:
    """Return the deterministic ordering key shared with the event queue."""
    return (
        to_nanos(event.timestamp),
        event.sequence,
        EVENT_CLASS_PRIORITY[event.event_type],
    )


def _cutoff_key(as_of: Nanos) -> tuple[Nanos, int, int]:
    """Return the inclusive upper bound for events at or before ``as_of``."""
    return (as_of, _MAX_TAIL, _MAX_TAIL)


def _start_key(start: Nanos) -> tuple[Nanos, int, int]:
    """Return the inclusive lower bound for events at or after ``start``."""
    return (start, 0, 0)


def _end_key(end: Nanos) -> tuple[Nanos, int, int]:
    """Return the exclusive upper bound for events before ``end``."""
    return (end - 1, _MAX_TAIL, _MAX_TAIL)


def _history_kinds(selector: EventSelector | None) -> tuple[EventType, ...]:
    """Resolve a selector to the history kinds it selects."""
    if selector is None:
        return _HISTORY_KINDS
    if isinstance(selector, EventType):
        if selector is EventType.QUOTE:
            return (EventType.QUOTE,)
        if selector is EventType.TRADE:
            return (EventType.TRADE,)
        raise ValueError("history retains quote and trade events only")
    if isinstance(selector, type) and issubclass(selector, Event):
        if issubclass(selector, QuoteEvent):
            return (EventType.QUOTE,)
        if issubclass(selector, TradeEvent):
            return (EventType.TRADE,)
        if selector is Event:
            return _HISTORY_KINDS
        raise ValueError("history retains quote and trade events only")
    raise TypeError("selector must be an Event class or EventType")


def _capacity_or(default: int, capacity: int | None) -> int:
    if capacity is None:
        return default
    return _validated_capacity(capacity)


def _validated_capacity(capacity: int) -> int:
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
        raise ValueError("capacity must be a positive integer")
    return capacity


class _RingBuffer[EventT: Event]:
    """Fixed-capacity ring buffer of ordered events with constant-time reads.

    Appends are amortized O(1) and evict the oldest retained event once full.
    Random access by logical index is O(1), which keeps binary searches and
    last-N reads proportional to the answer size rather than the history size.
    """

    __slots__ = ("_capacity", "_dropped", "_items", "_size", "_start")

    def __init__(self, capacity: int) -> None:
        self._capacity = _validated_capacity(capacity)
        self._items: list[EventT | None] = [None] * self._capacity
        self._start = 0
        self._size = 0
        self._dropped = 0

    @property
    def capacity(self) -> int:
        """Return the maximum number of retained events."""
        return self._capacity

    @property
    def dropped(self) -> int:
        """Return the number of events evicted by this buffer."""
        return self._dropped

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, index: int) -> EventT:
        size = self._size
        position = index + size if index < 0 else index
        if not 0 <= position < size:
            raise IndexError("ring buffer index out of range")
        item = self._items[(self._start + position) % self._capacity]
        assert item is not None
        return item

    def append(self, event: EventT) -> None:
        """Append one event, rejecting any ordering-key regression."""
        if self._size:
            key = _ordering_key(event)
            if key < _ordering_key(self[self._size - 1]):
                raise ValueError(
                    "event ordering key cannot precede the retained history"
                )
        index = (self._start + self._size) % self._capacity
        self._items[index] = event
        if self._size == self._capacity:
            self._start = (self._start + 1) % self._capacity
            self._dropped += 1
        else:
            self._size += 1

    def upper_bound(self, cutoff: tuple[Nanos, int, int]) -> int:
        """Return how many retained events sort at or before ``cutoff``."""
        return bisect_right(self, cutoff, key=_ordering_key)

    def latest(self, count: int, cutoff: tuple[Nanos, int, int]) -> tuple[EventT, ...]:
        """Return up to ``count`` newest events at or before ``cutoff``."""
        high = self.upper_bound(cutoff)
        if count <= 0 or high == 0:
            return ()
        low = high - count if count < high else 0
        return tuple(self[index] for index in range(low, high))

    def at(self, cutoff: tuple[Nanos, int, int]) -> EventT | None:
        """Return the newest event at or before ``cutoff``, if any."""
        index = self.upper_bound(cutoff) - 1
        return None if index < 0 else self[index]

    def between(
        self, start: tuple[Nanos, int, int], end: tuple[Nanos, int, int]
    ) -> tuple[EventT, ...]:
        """Return events in the half-open key range ``[start, end)``."""
        low = bisect_left(self, start, key=_ordering_key)
        high = bisect_right(self, end, key=_ordering_key)
        return tuple(self[index] for index in range(low, high))


@dataclass(frozen=True, slots=True)
class HistorySubscription:
    """One bounded ring-buffer subscription for a symbol and event selector."""

    symbol: Symbol
    selector: EventSelector
    capacity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        if not isinstance(self.selector, (type, EventType)):
            raise TypeError("selector must be an Event class or EventType")
        _history_kinds(self.selector)
        object.__setattr__(self, "capacity", _validated_capacity(self.capacity))

    @property
    def kinds(self) -> tuple[EventType, ...]:
        """Return the history kinds retained by this subscription."""
        return _history_kinds(self.selector)


@dataclass(frozen=True, slots=True)
class HistoryAggregate:
    """Exact point-in-time statistics for one half-open history window."""

    symbol: Symbol
    start: datetime
    end: datetime
    quote_count: int
    trade_count: int
    volume: Decimal
    notional: Decimal
    vwap: Decimal | None
    high: Decimal | None
    low: Decimal | None
    first_price: Decimal | None
    last_price: Decimal | None
    last_bid: Decimal | None
    last_ask: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        start_time = timestamp(self.start, "start")
        end_time = timestamp(self.end, "end")
        if end_time <= start_time:
            raise ValueError("end must be after start")
        object.__setattr__(self, "start", start_time)
        object.__setattr__(self, "end", end_time)
        for field_name in ("quote_count", "trade_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        for field_name in ("volume", "notional"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be a Decimal")
            if not value.is_finite() or value < _ZERO:
                raise ValueError(f"{field_name} must be a finite, non-negative Decimal")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("high must not be below low")


class HistoricalView:
    """Bounded, point-in-time market-data view clipped to the engine clock.

    The view owns one ring buffer per subscribed ``(symbol, kind)`` pair.  Reads
    never return an event newer than the injected clock head, and an explicit
    ``as_of`` newer than that head raises :class:`LookaheadError` before any
    data is touched.
    """

    __slots__ = ("_attached", "_buffers", "_capacity", "_clock", "_subscriptions")

    def __init__(
        self,
        clock: Clock,
        *,
        capacity: int = DEFAULT_HISTORY_CAPACITY,
        bus: EventBus | None = None,
        subscriptions: Iterable[HistorySubscription] = (),
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if bus is not None and not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        self._clock = clock
        self._capacity = _capacity_or(DEFAULT_HISTORY_CAPACITY, capacity)
        self._buffers: dict[tuple[Symbol, EventType], _RingBuffer[Event]] = {}
        self._subscriptions: list[HistorySubscription] = []
        self._attached: list[EventBus] = []
        for subscription in subscriptions:
            self._add(subscription)
        if bus is not None:
            self.attach(bus)

    @property
    def clock(self) -> Clock:
        """Return the injected engine clock."""
        return self._clock

    @property
    def capacity(self) -> int:
        """Return the default ring-buffer capacity for new subscriptions."""
        return self._capacity

    @property
    def subscriptions(self) -> tuple[HistorySubscription, ...]:
        """Return subscriptions in registration order."""
        return tuple(self._subscriptions)

    @property
    def head(self) -> datetime:
        """Return the current engine clock head, the hard read boundary."""
        return self._clock.now()

    @property
    def symbols(self) -> tuple[Symbol, ...]:
        """Return subscribed symbols in stable lexical order."""
        return tuple(
            sorted({subscription.symbol for subscription in self._subscriptions})
        )

    def subscribe(
        self,
        symbol: str | Symbol,
        selector: EventSelector | None = None,
        *,
        capacity: int | None = None,
    ) -> HistorySubscription:
        """Create bounded buffers for one symbol and selector.

        Re-subscribing the same symbol and selector keeps the retained history
        and widens the buffer only when a larger capacity is requested.
        """
        subscription = HistorySubscription(
            canonical_symbol(symbol),
            Event if selector is None else selector,
            _capacity_or(self._capacity, capacity),
        )
        existing = next(
            (
                candidate
                for candidate in self._subscriptions
                if candidate.symbol == subscription.symbol
                and candidate.selector == subscription.selector
            ),
            None,
        )
        if existing is not None and existing.capacity >= subscription.capacity:
            return existing
        if existing is not None:
            self._subscriptions.remove(existing)
        return self._add(subscription)

    def attach(self, bus: EventBus) -> None:
        """Ingest dispatched quotes and trades from a deterministic event bus."""
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        if any(attached is bus for attached in self._attached):
            return
        bus.subscribe(EventType.QUOTE, self._on_market)
        bus.subscribe(EventType.TRADE, self._on_market)
        self._attached.append(bus)

    def record(self, event: Event) -> None:
        """Retain one quote or trade event, rejecting future-dated events."""
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        if not isinstance(event, QuoteEvent | TradeEvent):
            raise ValueError("history retains quote and trade events only")
        self._guard(event)
        symbol = canonical_symbol(event.symbol)
        buffer = self._buffers.get((symbol, event.event_type))
        if buffer is not None:
            buffer.append(event)

    def record_many(self, events: Iterable[Event]) -> None:
        """Retain events in iteration order."""
        for event in events:
            self.record(event)

    def retained(
        self, symbol: str | Symbol, selector: EventSelector | None = None
    ) -> int:
        """Return how many events are currently retained for a subscription."""
        return sum(
            len(self._buffers[key])
            for key in self._keys(canonical_symbol(symbol), selector)
        )

    def dropped(
        self, symbol: str | Symbol, selector: EventSelector | None = None
    ) -> int:
        """Return how many events a subscription has evicted."""
        return sum(
            self._buffers[key].dropped
            for key in self._keys(canonical_symbol(symbol), selector)
        )

    def quote(
        self, symbol: str | Symbol, as_of: datetime | Nanos | None = None
    ) -> QuoteEvent | None:
        """Return the newest quote at or before ``as_of``, else ``None``."""
        return self._at_of(symbol, EventType.QUOTE, self._clip(as_of, "as_of"))

    def trade(
        self, symbol: str | Symbol, as_of: datetime | Nanos | None = None
    ) -> TradeEvent | None:
        """Return the newest trade at or before ``as_of``, else ``None``."""
        return self._at_of(symbol, EventType.TRADE, self._clip(as_of, "as_of"))

    def last(
        self,
        symbol: str | Symbol,
        count: int,
        selector: EventSelector | None = None,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[Event, ...]:
        """Return up to ``count`` newest matching events at or before ``as_of``."""
        size = self._count(count, "count")
        cutoff = self._clip(as_of, "as_of")
        return self._merge(
            self._buffers_for(canonical_symbol(symbol), selector), size, cutoff
        )

    def last_quotes(
        self,
        symbol: str | Symbol,
        count: int,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[QuoteEvent, ...]:
        """Return up to ``count`` newest quotes at or before ``as_of``."""
        return self._last_of(
            symbol,
            EventType.QUOTE,
            self._count(count, "count"),
            self._clip(as_of, "as_of"),
        )

    def last_trades(
        self,
        symbol: str | Symbol,
        count: int,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[TradeEvent, ...]:
        """Return up to ``count`` newest trades at or before ``as_of``."""
        return self._last_of(
            symbol,
            EventType.TRADE,
            self._count(count, "count"),
            self._clip(as_of, "as_of"),
        )

    def window(
        self,
        symbol: str | Symbol,
        start: datetime | Nanos,
        end: datetime | Nanos,
        selector: EventSelector | None = None,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[Event, ...]:
        """Return matching events in ``[start, end)``, clipped to the clock."""
        start_ns, end_ns = self._window(start, end, as_of)
        events = tuple(
            event
            for buffer in self._buffers_for(canonical_symbol(symbol), selector)
            for event in buffer.between(_start_key(start_ns), _end_key(end_ns))
        )
        return tuple(sorted(events, key=_ordering_key))

    def quotes(
        self,
        symbol: str | Symbol,
        start: datetime | Nanos,
        end: datetime | Nanos,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[QuoteEvent, ...]:
        """Return quotes in ``[start, end)``, clipped to the clock."""
        start_ns, end_ns = self._window(start, end, as_of)
        return self._window_of(symbol, EventType.QUOTE, start_ns, end_ns)

    def trades(
        self,
        symbol: str | Symbol,
        start: datetime | Nanos,
        end: datetime | Nanos,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[TradeEvent, ...]:
        """Return trades in ``[start, end)``, clipped to the clock."""
        start_ns, end_ns = self._window(start, end, as_of)
        return self._window_of(symbol, EventType.TRADE, start_ns, end_ns)

    def aggregate(
        self,
        symbol: str | Symbol,
        start: datetime | Nanos,
        end: datetime | Nanos,
        as_of: datetime | Nanos | None = None,
    ) -> HistoryAggregate:
        """Return exact trade and quote statistics for ``[start, end)``."""
        start_ns, end_ns = self._window(start, end, as_of)
        symbol_key = canonical_symbol(symbol)
        window_trades = self.trades(symbol_key, start_ns, end_ns)
        window_quotes = self.quotes(symbol_key, start_ns, end_ns)
        prices = tuple(trade.price for trade in window_trades)
        notional = sum(
            (trade.price * trade.quantity for trade in window_trades), start=_ZERO
        )
        volume = sum((trade.quantity for trade in window_trades), start=_ZERO)
        last_quote = window_quotes[-1] if window_quotes else None
        return HistoryAggregate(
            symbol=symbol_key,
            start=from_nanos(start_ns),
            end=from_nanos(end_ns),
            quote_count=len(window_quotes),
            trade_count=len(window_trades),
            volume=volume,
            notional=notional,
            vwap=None if not volume else notional / volume,
            high=max(prices) if prices else None,
            low=min(prices) if prices else None,
            first_price=prices[0] if prices else None,
            last_price=prices[-1] if prices else None,
            last_bid=None if last_quote is None else last_quote.bid_price,
            last_ask=None if last_quote is None else last_quote.ask_price,
        )

    def bars(
        self,
        symbol: str | Symbol,
        interval: timedelta | str,
        start: datetime | Nanos | None = None,
        end: datetime | Nanos | None = None,
        as_of: datetime | Nanos | None = None,
    ) -> tuple[OHLCVBar, ...]:
        """Return exact OHLCV bars over the retained trades in a window."""
        cutoff = self._clip(as_of, "as_of")
        start_ns = 0 if start is None else self._clip(start, "start")
        end_ns = cutoff if end is None else self._clip(end, "end")
        if end_ns < start_ns:
            raise ValueError("end must not be before start")
        aggregator = BarAggregator(interval_delta(interval), symbol=symbol)
        aggregator.add_many(self.trades(symbol, start_ns, end_ns))
        return aggregator.finalize()

    def read(
        self,
        timestamp: datetime | Nanos,
        symbol: str | Symbol,
        data_type: type[Event] | EventType | None = None,
    ) -> object:
        """Read the newest matching event no later than ``timestamp``.

        This implements the strategy-facing ``PointInTimeData`` boundary, so the
        same hard clipping applies to strategy code as to direct callers.
        """
        cutoff = self._clip(timestamp, "timestamp")
        cutoff_key = _cutoff_key(cutoff)
        best: Event | None = None
        best_key: tuple[Nanos, int, int] | None = None
        for key in self._keys(canonical_symbol(symbol), data_type):
            buffer = self._buffers.get(key)
            if buffer is None:
                continue
            candidate = buffer.at(cutoff_key)
            if candidate is None:
                continue
            candidate_key = _ordering_key(candidate)
            if best_key is None or candidate_key > best_key:
                best = candidate
                best_key = candidate_key
        return best

    def _on_market(self, event: Event) -> None:
        self.record(event)

    def _add(self, subscription: HistorySubscription) -> HistorySubscription:
        for kind in subscription.kinds:
            key = (subscription.symbol, kind)
            existing = self._buffers.get(key)
            if existing is not None and existing.capacity >= subscription.capacity:
                continue
            retained: tuple[Event, ...] = ()
            if existing is not None:
                retained = existing.latest(len(existing), _cutoff_key(_MAX_TAIL))
            replacement: _RingBuffer[Event] = _RingBuffer(subscription.capacity)
            for event in retained:
                replacement.append(event)
            self._buffers[key] = replacement
        self._subscriptions.append(subscription)
        return subscription

    def _guard(self, event: Event) -> None:
        head = to_nanos(self._clock.now(), "clock")
        stamp = to_nanos(event.timestamp)
        if stamp > head:
            raise LookaheadError(
                f"event {event.event_id!r} at {stamp} is newer than the "
                f"engine clock head {head}"
            )

    def _clip(self, value: datetime | Nanos | None, field_name: str) -> Nanos:
        head = to_nanos(self._clock.now(), "clock")
        if value is None:
            return head
        requested = to_nanos(value, field_name)
        if requested > head:
            raise LookaheadError(
                f"{field_name} {requested} is newer than the engine clock head {head}"
            )
        return requested

    def _keys(
        self, symbol: Symbol, selector: EventSelector | type[Event] | EventType | None
    ) -> tuple[tuple[Symbol, EventType], ...]:
        return tuple((symbol, kind) for kind in _history_kinds(selector))

    def _buffers_for(
        self, symbol: Symbol, selector: EventSelector | None
    ) -> tuple[_RingBuffer[Event], ...]:
        return tuple(
            self._buffers[key]
            for key in self._keys(symbol, selector)
            if key in self._buffers
        )

    def _merge(
        self,
        buffers: tuple[_RingBuffer[Event], ...],
        count: int,
        cutoff: Nanos,
    ) -> tuple[Event, ...]:
        if not buffers:
            return ()
        if len(buffers) == 1:
            return buffers[0].latest(count, _cutoff_key(cutoff))
        cutoff_key = _cutoff_key(cutoff)
        candidates = [
            event for buffer in buffers for event in buffer.latest(count, cutoff_key)
        ]
        candidates.sort(key=_ordering_key)
        return (
            tuple(candidates[-count:]) if count < len(candidates) else tuple(candidates)
        )

    def _window(
        self,
        start: datetime | Nanos,
        end: datetime | Nanos,
        as_of: datetime | Nanos | None,
    ) -> tuple[Nanos, Nanos]:
        start_ns = self._clip(start, "start")
        end_ns = self._clip(end, "end")
        self._clip(as_of, "as_of")
        if end_ns < start_ns:
            raise ValueError("end must not be before start")
        return start_ns, end_ns

    def _window_of[T: Event](
        self, symbol: str | Symbol, kind: EventType, start_ns: Nanos, end_ns: Nanos
    ) -> tuple[T, ...]:
        buffer = self._buffers.get((canonical_symbol(symbol), kind))
        if buffer is None:
            return ()
        events = buffer.between(_start_key(start_ns), _end_key(end_ns))
        return cast("tuple[T, ...]", events)

    def _last_of[T: Event](
        self, symbol: str | Symbol, kind: EventType, count: int, cutoff: Nanos
    ) -> tuple[T, ...]:
        buffer = self._buffers.get((canonical_symbol(symbol), kind))
        if buffer is None:
            return ()
        events = buffer.latest(count, _cutoff_key(cutoff))
        return cast("tuple[T, ...]", events)

    def _at_of[T: Event](
        self, symbol: str | Symbol, kind: EventType, cutoff: Nanos
    ) -> T | None:
        buffer = self._buffers.get((canonical_symbol(symbol), kind))
        if buffer is None:
            return None
        return cast("T | None", buffer.at(_cutoff_key(cutoff)))

    @staticmethod
    def _count(value: int, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return value


__all__ = [
    "DEFAULT_HISTORY_CAPACITY",
    "HistoryAggregate",
    "HistorySubscription",
    "HistoricalView",
    "LookaheadError",
    "Nanos",
    "from_nanos",
    "to_nanos",
]
