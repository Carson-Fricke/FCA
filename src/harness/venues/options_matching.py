"""Atomic multi-leg options matching on constituent quote updates.

Every group fills only when every leg is fillable at its full leg quantity on
one constituent quote update; partial fills never split legs.  Quotes arrive
per constituent symbol, so each leg is evaluated against the venue's latest
quote for that symbol and a quote update to any constituent symbol triggers
one all-or-nothing evaluation.  Staggered quotes therefore never partially
fill a group: they either complete it atomically on a later constituent
update or leave it entirely working.

Limit groups (``NET_DEBIT``/``NET_CREDIT``) fill every leg at the exact
on-tick quote midpoint produced by :class:`~harness.fills.mid_touch.MidTouch`
only when the natural (far-touch) leg combination is marketable through the
group limit and the midpoint combination satisfies the same net-limit rule
that :class:`~harness.oms.groups.MultiLegOrderGroup` enforces on commits.
Market groups fill every leg at the far touch.  Displayed touch size must
cover the full leg quantity, and concurrent groups consume that displayed
size in submission order.  ``IOC`` groups must fill at submission or expire
immediately; ``DAY`` and ``GTC`` groups work until filled or canceled.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from harness.core.clock import Clock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.common import text
from harness.core.types.events import Event, QuoteEvent
from harness.core.types.orders import MultiLegOrder, OrderLeg, Side, TimeInForce
from harness.fills.mid_touch import MidTouch
from harness.oms.groups import (
    GroupFillError,
    GroupOrderType,
    GroupSnapshot,
    MultiLegOMS,
    MultiLegOrderGroup,
)

_ZERO: Final = Decimal("0")
_ZERO_LATENCY: Final = timedelta(0)


class OptionsVenueError(ValueError):
    """Base class for deterministic options-matching venue errors."""


class DuplicateOptionsGroupError(OptionsVenueError):
    """Raised when a group identifier is submitted more than once."""


class UnknownOptionsGroupError(OptionsVenueError):
    """Raised when a command refers to an unknown multi-leg group."""


@dataclass(frozen=True, slots=True)
class _LegCandidate:
    """One fillable leg price candidate inside a single evaluation."""

    leg: OrderLeg
    quote: QuoteEvent
    price: Decimal


@dataclass(slots=True)
class _WorkingGroup:
    group: MultiLegOrderGroup
    order_type: GroupOrderType

    @property
    def is_active(self) -> bool:
        return self.group.snapshot.is_active


class OptionsMatchingEngine:
    """Match atomic multi-leg option groups against constituent quotes.

    A group is evaluated only on quote updates to one of its constituent
    symbols and fills only when every leg is fillable at its full quantity
    against the venue's latest quotes.  When an optional
    :class:`~harness.oms.groups.MultiLegOMS` is supplied, the engine drives
    the matching OMS lifecycle (submit, accept, begin evaluation, atomic
    fill, expire, cancel) with the same fill batch it schedules on the bus;
    each engine wired to one OMS must use a distinct ``venue`` name so fill
    identifiers remain unique.
    """

    __slots__ = (
        "_available",
        "_bus",
        "_clock",
        "_fill_model",
        "_groups",
        "_head",
        "_latest_quotes",
        "_latency",
        "_oms",
        "_ordinal",
        "_sequence",
        "_used_group_ids",
        "_venue",
    )

    def __init__(
        self,
        bus: EventBus,
        fill_model: MidTouch | None = None,
        latency: timedelta = _ZERO_LATENCY,
        *,
        clock: Clock | None = None,
        venue: str = "options",
        oms: MultiLegOMS | None = None,
    ) -> None:
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        if fill_model is not None and not isinstance(fill_model, MidTouch):
            raise TypeError("fill_model must be a MidTouch")
        if not isinstance(latency, timedelta):
            raise TypeError("latency must be a timedelta")
        if latency < _ZERO_LATENCY:
            raise ValueError("latency must not be negative")
        if clock is not None and not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if oms is not None and not isinstance(oms, MultiLegOMS):
            raise TypeError("oms must be a MultiLegOMS")
        self._bus = bus
        self._clock = clock
        self._fill_model = fill_model if fill_model is not None else MidTouch()
        self._latency = latency
        self._head: datetime | None = None
        self._sequence = -1
        self._ordinal = 0
        self._groups: dict[str, _WorkingGroup] = {}
        self._used_group_ids: set[str] = set()
        self._latest_quotes: dict[Symbol, QuoteEvent] = {}
        self._available: dict[Symbol, dict[Side, Decimal]] = {}
        self._venue = text(venue, "venue")
        self._oms = oms
        bus.subscribe(QuoteEvent, self._on_quote)
        bus.subscribe(Fill, self._on_fill)

    @property
    def latency(self) -> timedelta:
        """Return the deterministic response delay."""
        return self._latency

    @property
    def latest_quotes(self) -> tuple[QuoteEvent, ...]:
        """Return the latest quote per constituent symbol in feed order."""
        return tuple(self._latest_quotes.values())

    def submit(
        self,
        order: MultiLegOrder,
        order_type: GroupOrderType | str | None = None,
    ) -> None:
        """Accept a multi-leg group and evaluate it against known quotes."""
        if not isinstance(order, MultiLegOrder):
            raise TypeError("order must be a MultiLegOrder")
        if order.order_id in self._used_group_ids:
            raise DuplicateOptionsGroupError(
                f"group order_id {order.order_id!r} was already used"
            )
        group = MultiLegOrderGroup(order, order_type)
        group.accept()
        self._used_group_ids.add(order.order_id)
        working = _WorkingGroup(group=group, order_type=group.snapshot.order_type)
        self._groups[order.order_id] = working
        if self._oms is not None:
            self._oms.submit(order, order_type)
            self._oms.accept(order.order_id)
        self._observe(order)
        if not self._evaluate(working) and order.time_in_force is TimeInForce.IOC:
            self._expire(working)

    def cancel(self, group_id: str) -> None:
        """Cancel an active group and propagate cancellation to the OMS."""
        working = self._working(group_id)
        working.group.cancel()
        if self._oms is not None:
            self._oms.cancel(group_id)

    def state(self, group_id: str) -> GroupSnapshot:
        """Return the immutable engine-side snapshot for a group."""
        return self._working(group_id).group.snapshot

    def states(self) -> tuple[GroupSnapshot, ...]:
        """Return group snapshots in deterministic submission order."""
        return tuple(working.group.snapshot for working in self._groups.values())

    def fills_for(self, group_id: str) -> tuple[Fill, ...]:
        """Return the committed atomic fill batch in original leg order."""
        return self._working(group_id).group.fills

    def latest_quote(self, symbol: str | Symbol) -> QuoteEvent | None:
        """Return the latest quote observed for one symbol."""
        return self._latest_quotes.get(Symbol(str(symbol)))

    def _on_quote(self, event: QuoteEvent) -> None:
        self._observe(event)
        self._latest_quotes[event.symbol] = event
        self._available[event.symbol] = {
            Side.BUY: event.ask_quantity,
            Side.SELL: event.bid_quantity,
        }
        for working in tuple(self._groups.values()):
            if not working.is_active:
                continue
            if any(leg.symbol == event.symbol for leg in working.group.order.legs):
                self._evaluate(working)

    def _on_fill(self, event: Fill) -> None:
        self._observe(event)

    def _evaluate(self, working: _WorkingGroup) -> bool:
        group = working.group
        candidate: list[_LegCandidate] = []
        for leg in group.order.legs:
            quote = self._latest_quotes.get(leg.symbol)
            if quote is None:
                return False
            pool = self._available.get(leg.symbol)
            if pool is None or pool.get(leg.side, _ZERO) < leg.quantity:
                return False
            price = self._leg_price(working.order_type, leg, quote)
            if price is None:
                return False
            candidate.append(_LegCandidate(leg=leg, quote=quote, price=price))
        if not self._net_limit(working.order_type, group.order, candidate):
            return False
        return self._commit(working, candidate) is not None

    def _leg_price(
        self, order_type: GroupOrderType, leg: OrderLeg, quote: QuoteEvent
    ) -> Decimal | None:
        if order_type is GroupOrderType.MARKET:
            return self._touch_price(leg, quote)
        return self._fill_model.mid_execution(quote)

    @staticmethod
    def _touch_price(leg: OrderLeg, quote: QuoteEvent) -> Decimal:
        return quote.ask_price if leg.side is Side.BUY else quote.bid_price

    def _net_limit(
        self,
        order_type: GroupOrderType,
        order: MultiLegOrder,
        candidate: list[_LegCandidate],
    ) -> bool:
        if order_type is GroupOrderType.MARKET:
            return True
        limit = order.limit_price
        assert limit is not None
        natural = self._net(
            (entry.leg, self._touch_price(entry.leg, entry.quote))
            for entry in candidate
        )
        mid_net = self._net((entry.leg, entry.price) for entry in candidate)
        if order_type is GroupOrderType.NET_DEBIT:
            if natural > limit or mid_net <= _ZERO or mid_net > limit:
                return False
            return True
        if natural > -limit or mid_net >= _ZERO or -mid_net < limit:
            return False
        return True

    def _commit(
        self, working: _WorkingGroup, candidate: list[_LegCandidate]
    ) -> tuple[Fill, ...] | None:
        group = working.group
        timestamp = self._response_timestamp()
        liquidity = "taker" if working.order_type is GroupOrderType.MARKET else "maker"
        fills: list[Fill] = []
        for entry in candidate:
            fill_id = self._next_id("fill")
            fills.append(
                Fill(
                    event_id=fill_id,
                    timestamp=timestamp,
                    sequence=self._next_sequence(),
                    fill_id=fill_id,
                    order_id=group.order.order_id,
                    symbol=entry.leg.symbol,
                    side=entry.leg.side,
                    quantity=entry.leg.quantity,
                    price=entry.price,
                    liquidity=liquidity,
                )
            )
        try:
            group.begin_evaluation()
            group.fill(fills)
        except GroupFillError:
            group.end_evaluation()
            return None
        if self._oms is not None:
            self._oms.begin_evaluation(group.order.order_id)
            self._oms.fill(group.order.order_id, fills)
        for entry in candidate:
            pool = self._available[entry.leg.symbol]
            pool[entry.leg.side] = pool[entry.leg.side] - entry.leg.quantity
        for fill in fills:
            self._bus.schedule(fill)
        return tuple(fills)

    def _expire(self, working: _WorkingGroup) -> None:
        working.group.expire()
        if self._oms is not None:
            self._oms.expire(working.group.order.order_id)

    def _net(self, terms: Iterable[tuple[OrderLeg, Decimal]]) -> Decimal:
        net = _ZERO
        for leg, price in terms:
            weighted = price * leg.ratio
            net = net + weighted if leg.side is Side.BUY else net - weighted
        return net

    def _working(self, group_id: str) -> _WorkingGroup:
        try:
            return self._groups[group_id]
        except KeyError as error:
            raise UnknownOptionsGroupError(group_id) from error

    def _observe(self, event: Event) -> None:
        if self._head is None or event.timestamp > self._head:
            self._head = event.timestamp
        self._sequence = max(self._sequence, event.sequence)

    def _response_timestamp(self) -> datetime:
        head = self._head
        if self._clock is not None:
            clock_head = self._clock.now()
            head = clock_head if head is None else max(head, clock_head)
        if head is None:
            raise OptionsVenueError("a timestamped order or quote event is required")
        return head + self._latency

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _next_id(self, kind: str) -> str:
        self._ordinal += 1
        return f"{self._venue}-{kind}-{self._ordinal}"


OptionsMatchingVenue = OptionsMatchingEngine
OptionsVenue = OptionsMatchingEngine
MultiLegOptionsVenue = OptionsMatchingEngine

__all__ = [
    "DuplicateOptionsGroupError",
    "OptionsMatchingEngine",
    "OptionsMatchingVenue",
    "OptionsVenue",
    "OptionsVenueError",
    "UnknownOptionsGroupError",
]
