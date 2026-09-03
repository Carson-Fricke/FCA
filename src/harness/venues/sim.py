"""Synchronous deterministic resting-order venue for top-of-book replay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from harness.core.clock import Clock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import ExecutionReport, Fill
from harness.core.types.common import text
from harness.core.types.events import Event, QuoteEvent, TradeEvent
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.fills.simple_touch import FillDecision, SimpleTouch

_ZERO = Decimal("0")
type MarketEvent = QuoteEvent | TradeEvent
type FillPlugin = Callable[..., FillDecision | None]


class SimVenueError(ValueError):
    """Base class for deterministic simulated-venue command errors."""


class UnknownSimOrderError(SimVenueError):
    """Raised when a command refers to an unknown simulated order."""


class DuplicateSimOrderError(SimVenueError):
    """Raised when an order identifier is submitted more than once."""


@dataclass(frozen=True, slots=True)
class SimOrderState:
    """Immutable inspection snapshot of one venue-side order."""

    order: SingleLegOrder
    status: OrderStatus
    filled_quantity: Decimal
    remaining_quantity: Decimal
    armed: bool
    triggering_market: MarketEvent | None
    decision_price: Decimal | None
    last_decision: FillDecision | None

    @property
    def order_id(self) -> str:
        """Return the order identifier."""
        return self.order.order_id

    @property
    def is_active(self) -> bool:
        """Return whether the venue can still match or cancel the order."""
        return self.status not in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
        }

    @property
    def triggering_market_snapshot(self) -> MarketEvent | None:
        """Return the immutable stop or fill trigger snapshot."""
        return self.triggering_market

    @property
    def decision(self) -> FillDecision | None:
        """Return the last immutable fill decision."""
        return self.last_decision

    @property
    def triggered(self) -> bool:
        """Return whether a stop order has crossed its trigger."""
        return (
            not self.armed
            if self.order.order_type
            in {
                OrderType.STOP,
                OrderType.STOP_LIMIT,
            }
            else True
        )


@dataclass(slots=True)
class _WorkingOrder:
    order: SingleLegOrder
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: Decimal = _ZERO
    filled_notional: Decimal = _ZERO
    armed: bool = False
    stop_triggered: bool = False
    triggering_market: MarketEvent | None = None
    decision_price: Decimal | None = None
    last_decision: FillDecision | None = None
    response_pending: bool = False

    @property
    def remaining_quantity(self) -> Decimal:
        return self.order.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status not in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
        }


class SimulatedVenue:
    """Match single-leg orders against explicit quote/trade events.

    Commands return no outcomes.  Acknowledgements, fills, cancellations,
    expiries, and replacement reports are all inserted into the supplied
    :class:`~harness.core.events.EventBus` with explicit timestamps.
    """

    __slots__ = (
        "_bus",
        "_clock",
        "_fill_model",
        "_head",
        "_latency",
        "_orders",
        "_used_order_ids",
        "_sequence",
        "_ordinal",
        "_latest_market",
        "_latest_markets",
        "_decisions",
        "_venue",
    )

    def __init__(
        self,
        bus: EventBus,
        fill_model: FillPlugin | None = None,
        latency: timedelta = timedelta(0),
        *,
        fill_plugin: FillPlugin | None = None,
        clock: Clock | None = None,
        venue: str = "sim",
    ) -> None:
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        if fill_model is not None and fill_plugin is not None:
            raise TypeError("provide only one of fill_model or fill_plugin")
        selected_model = fill_model if fill_model is not None else fill_plugin
        if selected_model is None:
            selected_model = SimpleTouch()
        if not callable(selected_model):
            raise TypeError("fill_model must be callable")
        if not isinstance(latency, timedelta):
            raise TypeError("latency must be a timedelta")
        if latency < timedelta(0):
            raise ValueError("latency must not be negative")
        if clock is not None and not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        self._bus = bus
        self._clock = clock
        self._fill_model = selected_model
        self._head: datetime | None = None
        self._latency = latency
        self._orders: dict[str, _WorkingOrder] = {}
        self._used_order_ids: set[str] = set()
        self._sequence = -1
        self._ordinal = 0
        self._latest_market: MarketEvent | None = None
        self._latest_markets: dict[Symbol, MarketEvent] = {}
        self._decisions: list[FillDecision] = []
        self._venue = text(venue, "venue")
        bus.subscribe(QuoteEvent, self._on_market)
        bus.subscribe(TradeEvent, self._on_market)
        bus.subscribe(ExecutionReport, self._on_report)
        bus.subscribe(Fill, self._on_fill)

    @property
    def latency(self) -> timedelta:
        """Return the deterministic response delay."""
        return self._latency

    @property
    def latest_market(self) -> MarketEvent | None:
        """Return the latest immutable market snapshot observed by the venue."""
        return self._latest_market

    @property
    def decisions(self) -> tuple[FillDecision, ...]:
        """Return fill decisions in deterministic matching order."""
        return tuple(self._decisions)

    @property
    def active_orders(self) -> tuple[SimOrderState, ...]:
        """Return active order snapshots in submission order."""
        return tuple(
            self._snapshot(order) for order in self._orders.values() if order.is_active
        )

    def submit(self, order: SingleLegOrder) -> None:
        """Accept an order and schedule its acknowledgement and outcome."""
        self._validate_order(order)
        if order.order_id in self._used_order_ids:
            raise DuplicateSimOrderError(
                f"order_id {order.order_id!r} was already used"
            )
        self._observe(order)
        working = _WorkingOrder(
            order=order,
            status=OrderStatus.ACCEPTED,
            armed=order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT),
        )
        self._orders[order.order_id] = working
        self._used_order_ids.add(order.order_id)
        self._schedule_report(working, ExecutionType.ACKNOWLEDGED, OrderStatus.ACCEPTED)
        latest_market = self._latest_markets.get(order.symbol)
        if latest_market is not None and not working.armed:
            decision = self._attempt(working, latest_market)
            if (
                decision is None
                and order.time_in_force is TimeInForce.IOC
                and self._is_matching_opportunity(working, latest_market)
            ):
                self._schedule_expiry(working)

    def cancel(self, order_id: str) -> None:
        """Schedule cancellation confirmation for an active non-pending order."""
        working = self._working(order_id)
        if not working.is_active:
            raise SimVenueError(f"order {order_id!r} is not active")
        if working.response_pending:
            raise SimVenueError("cannot cancel while an execution outcome is pending")
        working.status = OrderStatus.CANCELED
        working.response_pending = True
        self._schedule_report(working, ExecutionType.CANCELED, OrderStatus.CANCELED)

    def replace(self, order_id: str, replacement: SingleLegOrder) -> None:
        """Atomically report replacement and begin the replacement order."""
        self._validate_order(replacement)
        working = self._working(order_id)
        if not working.is_active:
            raise SimVenueError(f"order {order_id!r} is not active")
        if working.response_pending:
            raise SimVenueError("cannot replace while an execution outcome is pending")
        if replacement.order_id in self._used_order_ids:
            raise DuplicateSimOrderError(
                f"order_id {replacement.order_id!r} was already used"
            )
        if replacement.symbol != working.order.symbol:
            raise SimVenueError("replacement cannot change symbol")
        if replacement.side is not working.order.side:
            raise SimVenueError("replacement cannot change side")

        working.status = OrderStatus.REPLACED
        working.response_pending = True
        self._used_order_ids.add(replacement.order_id)
        self._schedule_report(working, ExecutionType.REPLACED, OrderStatus.REPLACED)

        replacement_working = _WorkingOrder(
            order=replacement,
            status=OrderStatus.ACCEPTED,
            armed=replacement.order_type in (OrderType.STOP, OrderType.STOP_LIMIT),
        )
        self._orders[replacement.order_id] = replacement_working
        self._schedule_report(
            replacement_working,
            ExecutionType.ACKNOWLEDGED,
            OrderStatus.ACCEPTED,
        )
        latest_market = self._latest_markets.get(replacement.symbol)
        if latest_market is not None and not replacement_working.armed:
            decision = self._attempt(replacement_working, latest_market)
            if (
                decision is None
                and replacement.time_in_force is TimeInForce.IOC
                and self._is_matching_opportunity(replacement_working, latest_market)
            ):
                self._schedule_expiry(replacement_working)

    def request_position_snapshot(self, account_id: str) -> None:
        """Validate the protocol command; positions belong to the accounting hub."""
        text(account_id, "account_id")

    def state(self, order_id: str) -> SimOrderState:
        """Return an immutable venue-side state snapshot."""
        return self._snapshot(self._working(order_id))

    def order_state(self, order_id: str) -> SimOrderState:
        """Alias for :meth:`state`."""
        return self.state(order_id)

    def decisions_for(self, order_id: str) -> tuple[FillDecision, ...]:
        """Return all decisions made for one order."""
        self._working(order_id)
        return tuple(
            decision for decision in self._decisions if decision.order_id == order_id
        )

    def _on_market(self, event: MarketEvent) -> None:
        self._observe(event)
        self._latest_market = event
        self._latest_markets[event.symbol] = event
        available = {
            Side.BUY: event.quantity
            if isinstance(event, TradeEvent)
            else event.ask_quantity,
            Side.SELL: event.quantity
            if isinstance(event, TradeEvent)
            else event.bid_quantity,
        }
        for working in tuple(self._orders.values()):
            if not working.is_active or working.response_pending:
                continue
            if working.order.symbol != event.symbol:
                continue
            decision = self._attempt(
                working,
                event,
                available[working.order.side],
            )
            if decision is None and self._is_matching_opportunity(working, event):
                if working.order.time_in_force is TimeInForce.IOC:
                    self._schedule_expiry(working)
            elif decision is not None:
                available[working.order.side] -= decision.quantity

    def _attempt(
        self,
        working: _WorkingOrder,
        market: MarketEvent,
        available_quantity: Decimal | None = None,
    ) -> FillDecision | None:
        if working.remaining_quantity <= 0:
            return None
        if working.order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if not working.stop_triggered:
                if not self._is_triggered(working.order, market):
                    return None
                working.stop_triggered = True
                working.armed = False
                working.triggering_market = market
        decision = self._decide(
            working,
            market,
            available_quantity=available_quantity,
        )
        if decision is None:
            return None
        if not isinstance(decision, FillDecision):
            raise SimVenueError("fill plugin must return a FillDecision or None")
        if (
            working.order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
            and working.triggering_market is not None
            and decision.triggering_market is not working.triggering_market
        ):
            decision = replace(
                decision,
                triggering_market=working.triggering_market,
            )
        working.triggering_market = decision.triggering_market
        working.decision_price = decision.decision_price
        working.last_decision = decision
        self._decisions.append(decision)
        self._schedule_fill(working, decision)
        return decision

    def _decide(
        self,
        working: _WorkingOrder,
        market: MarketEvent,
        *,
        available_quantity: Decimal | None,
    ) -> FillDecision | None:
        try:
            return self._fill_model(
                working.order,
                market,
                working.remaining_quantity,
                available_quantity=available_quantity,
                stop_triggered=working.stop_triggered,
            )
        except TypeError:
            # A deliberately small third-party plugin may only accept the
            # order, market, and remaining quantity.  It remains deterministic.
            return self._fill_model(working.order, market, working.remaining_quantity)

    def _schedule_fill(self, working: _WorkingOrder, decision: FillDecision) -> None:
        if decision.order_id != working.order.order_id:
            raise SimVenueError("fill decision order_id does not match working order")
        if decision.quantity > working.remaining_quantity:
            raise SimVenueError("fill decision exceeds the order remainder")
        working.filled_quantity += decision.quantity
        working.filled_notional += decision.quantity * decision.price
        working.status = (
            OrderStatus.FILLED
            if working.remaining_quantity == _ZERO
            else OrderStatus.PARTIALLY_FILLED
        )
        working.response_pending = True
        status = working.status
        execution_type = (
            ExecutionType.FILL
            if status is OrderStatus.FILLED
            else ExecutionType.PARTIAL_FILL
        )
        self._schedule_report(
            working,
            execution_type,
            status,
            last_fill_quantity=decision.quantity,
            last_fill_price=decision.price,
        )
        timestamp = self._response_timestamp()
        fill_id = self._next_id("fill")
        self._bus.schedule(
            Fill(
                event_id=fill_id,
                timestamp=timestamp,
                sequence=self._next_sequence(),
                fill_id=fill_id,
                order_id=working.order.order_id,
                symbol=working.order.symbol,
                side=working.order.side,
                quantity=decision.quantity,
                price=decision.price,
                liquidity=decision.liquidity,
            )
        )
        if (
            status is OrderStatus.PARTIALLY_FILLED
            and working.order.time_in_force is TimeInForce.IOC
        ):
            self._schedule_expiry(working, allow_pending=True)

    def _schedule_expiry(
        self, working: _WorkingOrder, *, allow_pending: bool = False
    ) -> None:
        if not working.is_active or (working.response_pending and not allow_pending):
            return
        working.status = OrderStatus.EXPIRED
        working.response_pending = True
        self._schedule_report(working, ExecutionType.EXPIRED, OrderStatus.EXPIRED)

    def _schedule_report(
        self,
        working: _WorkingOrder,
        execution_type: ExecutionType,
        status: OrderStatus,
        *,
        last_fill_quantity: Decimal = _ZERO,
        last_fill_price: Decimal | None = None,
    ) -> None:
        timestamp = self._response_timestamp()
        report_id = self._next_id("report")
        average = (
            None
            if working.filled_quantity == _ZERO
            else working.filled_notional / working.filled_quantity
        )
        decision_text = ""
        if working.last_decision is not None:
            decision_text = (
                f"decision_price={working.last_decision.decision_price};"
                f"triggering_market={working.last_decision.triggering_market.event_id}"
            )
        self._bus.schedule(
            ExecutionReport(
                event_id=report_id,
                timestamp=timestamp,
                sequence=self._next_sequence(),
                report_id=report_id,
                order_id=working.order.order_id,
                status=status,
                execution_type=execution_type,
                order_quantity=working.order.quantity,
                filled_quantity=working.filled_quantity,
                remaining_quantity=working.remaining_quantity,
                average_fill_price=average,
                last_fill_quantity=last_fill_quantity,
                last_fill_price=last_fill_price,
                text=decision_text or None,
            )
        )

    def _on_report(self, event: ExecutionReport) -> None:
        self._observe(event)
        working = self._orders.get(event.order_id)
        if (
            working is not None
            and event.execution_type is not ExecutionType.ACKNOWLEDGED
        ):
            working.response_pending = False

    def _on_fill(self, event: Fill) -> None:
        self._observe(event)

    def _is_matching_opportunity(
        self, working: _WorkingOrder, market: MarketEvent
    ) -> bool:
        if working.order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            return working.stop_triggered or self._is_triggered(working.order, market)
        return True

    def _is_triggered(self, order: SingleLegOrder, market: MarketEvent) -> bool:
        model = self._fill_model
        if isinstance(model, SimpleTouch):
            return model.is_triggered(order, market)
        if order.stop_price is None:
            return False
        if isinstance(market, TradeEvent):
            price = market.price
        elif order.side is Side.BUY:
            price = market.ask_price
        else:
            price = market.bid_price
        return (
            price >= order.stop_price
            if order.side is Side.BUY
            else price <= order.stop_price
        )

    @staticmethod
    def _validate_order(order: SingleLegOrder) -> None:
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be a SingleLegOrder")

    def _working(self, order_id: str) -> _WorkingOrder:
        try:
            return self._orders[order_id]
        except KeyError as error:
            raise UnknownSimOrderError(order_id) from error

    def _snapshot(self, working: _WorkingOrder) -> SimOrderState:
        return SimOrderState(
            order=working.order,
            status=working.status,
            filled_quantity=working.filled_quantity,
            remaining_quantity=working.remaining_quantity,
            armed=working.armed,
            triggering_market=working.triggering_market,
            decision_price=working.decision_price,
            last_decision=working.last_decision,
        )

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
            raise SimVenueError("a timestamped order or market event is required")
        return head + self._latency

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _next_id(self, kind: str) -> str:
        self._ordinal += 1
        return f"{self._venue}-{kind}-{self._ordinal}"


SimVenue = SimulatedVenue
SimulatedExecutionVenue = SimulatedVenue
MatchingEngine = SimulatedVenue
SimulatedMatchingEngine = SimulatedVenue

__all__ = [
    "DuplicateSimOrderError",
    "FillPlugin",
    "MatchingEngine",
    "MarketEvent",
    "SimOrderState",
    "SimVenue",
    "SimVenueError",
    "SimulatedExecutionVenue",
    "SimulatedMatchingEngine",
    "SimulatedVenue",
    "UnknownSimOrderError",
]
