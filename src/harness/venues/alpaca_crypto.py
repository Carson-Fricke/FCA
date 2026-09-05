"""Deterministic fractional crypto execution venue for Alpaca accounts.

The venue runs continuously on every UTC day, so commands never consult a
session calendar.  Quantities stay exact ``Decimal`` values through the full
submit/cancel/replace command path, and every fill is reconciled against the
configured maker-taker fee model and the venue fee schedule before any event
reaches the bus.  The synchronous kernel performs no network, wall-clock, or
random access: outcomes are scheduled with explicit event timestamps.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import ClassVar, Final

from harness.core.clock import Clock
from harness.core.events import EventBus
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.accounting import ExecutionReport, Fill
from harness.core.types.common import exact_decimal, text
from harness.core.types.events import Event, QuoteEvent, TradeEvent
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.fills.fees import (
    CryptoMakerTakerFeeModel,
    FeeRounding,
    LiquidityRole,
    RoundingMode,
)
from harness.fills.simple_touch import FillDecision, SimpleTouch

_ZERO: Final = Decimal("0")
_BASIS_POINTS: Final = Decimal("10000")
_SCHEDULE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")
_SCHEDULE_FIELDS: Final = {
    "schedule_id",
    "currency",
    "maker_rate_bps",
    "taker_rate_bps",
    "rounding",
}
_ROUNDING_FIELDS: Final = {"precision", "mode"}

type MarketEvent = QuoteEvent | TradeEvent
type FillPlugin = Callable[..., FillDecision | None]


class AlpacaCryptoVenueError(ValueError):
    """Base class for deterministic crypto-venue command errors."""


class UnknownCryptoOrderError(AlpacaCryptoVenueError):
    """Raised when a command refers to an unknown crypto order."""


class DuplicateCryptoOrderError(AlpacaCryptoVenueError):
    """Raised when an order identifier is submitted more than once."""


class AlpacaCryptoFeeMismatchError(AlpacaCryptoVenueError):
    """Raised when configured fees disagree with the venue fee schedule."""


def is_crypto_pair(symbol: str | Symbol) -> bool:
    """Return whether a canonical symbol is a ``BASE/QUOTE`` crypto pair."""
    base, separator, quote = str(symbol).partition("/")
    return bool(separator and base and quote and "/" not in quote)


@dataclass(frozen=True, slots=True)
class AlpacaCryptoFeeSchedule:
    """A venue-reported maker-taker crypto fee schedule fixture.

    The schedule stands in for the fee schedule fetched from the venue.  Its
    rates and rounding are independent of the locally configured fee model so
    that per-fill reconciliation can surface real disagreements instead of
    silently tolerating them.
    """

    schedule_id: str
    currency: str
    maker_rate_bps: Decimal
    taker_rate_bps: Decimal
    rounding: FeeRounding

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule_id", _identifier(self.schedule_id))
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self, "maker_rate_bps", _nonnegative_decimal(self.maker_rate_bps)
        )
        object.__setattr__(
            self, "taker_rate_bps", _nonnegative_decimal(self.taker_rate_bps)
        )
        if not isinstance(self.rounding, FeeRounding):
            raise TypeError("rounding must be FeeRounding")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> AlpacaCryptoFeeSchedule:
        """Build one schedule from a strict fixture mapping."""
        mapping = _mapping(values, "fee schedule")
        _keys(mapping, _SCHEDULE_FIELDS, "fee schedule")
        rounding_values = _mapping(mapping["rounding"], "rounding")
        _keys(rounding_values, _ROUNDING_FIELDS, "rounding")
        return cls(
            schedule_id=_identifier(mapping["schedule_id"]),
            currency=_currency(mapping["currency"]),
            maker_rate_bps=_nonnegative_decimal(mapping["maker_rate_bps"]),
            taker_rate_bps=_nonnegative_decimal(mapping["taker_rate_bps"]),
            rounding=FeeRounding(
                precision=_decimal_value(rounding_values["precision"]),
                mode=_rounding_mode(rounding_values["mode"]),
            ),
        )

    def rate_for(self, liquidity: LiquidityRole | str) -> Decimal:
        """Return the schedule rate in basis points for one liquidity role."""
        role = LiquidityRole(liquidity)
        return (
            self.maker_rate_bps if role is LiquidityRole.MAKER else self.taker_rate_bps
        )

    def charge(
        self, quantity: Decimal, price: Decimal, liquidity: LiquidityRole | str
    ) -> Decimal:
        """Return the venue-reported charge for one fill, exactly rounded."""
        notional = exact_decimal(quantity, "quantity") * exact_decimal(price, "price")
        return self.rounding.apply(notional * self.rate_for(liquidity) / _BASIS_POINTS)


@dataclass(frozen=True, slots=True)
class CryptoFeeLedgerEntry:
    """One reconciled local fee record for a single fill."""

    fill_id: str
    order_id: str
    symbol: Symbol
    side: Side
    quantity: Decimal
    price: Decimal
    liquidity: str
    fee: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", text(self.fill_id, "fill_id"))
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, "quantity"))
        object.__setattr__(self, "price", exact_decimal(self.price, "price"))
        object.__setattr__(self, "liquidity", LiquidityRole(self.liquidity).value)
        object.__setattr__(self, "fee", exact_decimal(self.fee, "fee"))


@dataclass(frozen=True, slots=True)
class CryptoOrderState:
    """Immutable inspection snapshot of one venue-side crypto order."""

    order: SingleLegOrder
    status: OrderStatus
    filled_quantity: Decimal
    remaining_quantity: Decimal
    fees_paid: Decimal
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


@dataclass(slots=True)
class _WorkingOrder:
    order: SingleLegOrder
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: Decimal = _ZERO
    filled_notional: Decimal = _ZERO
    fees_paid: Decimal = _ZERO
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


class AlpacaCryptoVenue:
    """Execute fractional crypto orders continuously on every UTC day.

    Commands never consult a session calendar: crypto markets trade 24/7, so
    submit, cancel, and replace are valid at any explicit engine timestamp.
    Submit-time fills against the already visible book are taker fills, and
    fills of resting orders against later market events are maker fills.  Each
    fill is reconciled against the configured maker-taker fee model and the
    venue fee schedule before it is scheduled, and a mismatch raises
    :class:`AlpacaCryptoFeeMismatchError` instead of recording a fee.
    """

    continuous: ClassVar[bool] = True

    __slots__ = (
        "_bus",
        "_clock",
        "_decisions",
        "_fee_model",
        "_fee_schedule",
        "_fill_model",
        "_head",
        "_latency",
        "_latest_market",
        "_latest_markets",
        "_ledger",
        "_orders",
        "_ordinal",
        "_sequence",
        "_used_order_ids",
        "_venue",
    )

    def __init__(
        self,
        bus: EventBus,
        *,
        fee_model: CryptoMakerTakerFeeModel,
        fee_schedule: AlpacaCryptoFeeSchedule,
        latency: timedelta = timedelta(0),
        clock: Clock | None = None,
        venue: str = "alpaca-crypto",
        fill_model: FillPlugin | None = None,
    ) -> None:
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        if not isinstance(fee_model, CryptoMakerTakerFeeModel):
            raise TypeError("fee_model must be a CryptoMakerTakerFeeModel")
        if not isinstance(fee_schedule, AlpacaCryptoFeeSchedule):
            raise TypeError("fee_schedule must be an AlpacaCryptoFeeSchedule")
        if not isinstance(latency, timedelta):
            raise TypeError("latency must be a timedelta")
        if latency < timedelta(0):
            raise ValueError("latency must not be negative")
        if clock is not None and not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        selected_model = fill_model if fill_model is not None else SimpleTouch()
        if not callable(selected_model):
            raise TypeError("fill_model must be callable")
        self._bus = bus
        self._clock = clock
        self._fee_model = fee_model
        self._fee_schedule = fee_schedule
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
        self._ledger: list[CryptoFeeLedgerEntry] = []
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
    def fee_ledger(self) -> tuple[CryptoFeeLedgerEntry, ...]:
        """Return reconciled local fee records in fill order."""
        return tuple(self._ledger)

    @property
    def active_orders(self) -> tuple[CryptoOrderState, ...]:
        """Return active order snapshots in submission order."""
        return tuple(
            self._snapshot(order) for order in self._orders.values() if order.is_active
        )

    def submit(self, order: SingleLegOrder) -> None:
        """Accept a fractional order and schedule its venue outcomes."""
        self._validate_order(order)
        if order.order_id in self._used_order_ids:
            raise DuplicateCryptoOrderError(
                f"order_id {order.order_id!r} was already used"
            )
        self._observe(order)
        working = _WorkingOrder(order=order, status=OrderStatus.ACCEPTED)
        self._orders[order.order_id] = working
        self._used_order_ids.add(order.order_id)
        self._schedule_report(working, ExecutionType.ACKNOWLEDGED, OrderStatus.ACCEPTED)
        latest_market = self._latest_markets.get(order.symbol)
        if latest_market is not None:
            decision = self._attempt(working, latest_market, LiquidityRole.TAKER)
            if decision is None and order.time_in_force is TimeInForce.IOC:
                self._schedule_expiry(working)

    def cancel(self, order_id: str) -> None:
        """Schedule cancellation confirmation for an active non-pending order."""
        working = self._working(order_id)
        if not working.is_active:
            raise AlpacaCryptoVenueError(f"order {order_id!r} is not active")
        if working.response_pending:
            raise AlpacaCryptoVenueError(
                "cannot cancel while an execution outcome is pending"
            )
        working.status = OrderStatus.CANCELED
        working.response_pending = True
        self._schedule_report(working, ExecutionType.CANCELED, OrderStatus.CANCELED)

    def replace(self, order_id: str, replacement: SingleLegOrder) -> None:
        """Atomically report replacement and begin the replacement order."""
        self._validate_order(replacement)
        working = self._working(order_id)
        if not working.is_active:
            raise AlpacaCryptoVenueError(f"order {order_id!r} is not active")
        if working.response_pending:
            raise AlpacaCryptoVenueError(
                "cannot replace while an execution outcome is pending"
            )
        if replacement.order_id in self._used_order_ids:
            raise DuplicateCryptoOrderError(
                f"order_id {replacement.order_id!r} was already used"
            )
        if replacement.symbol != working.order.symbol:
            raise AlpacaCryptoVenueError("replacement cannot change symbol")
        if replacement.side is not working.order.side:
            raise AlpacaCryptoVenueError("replacement cannot change side")

        working.status = OrderStatus.REPLACED
        working.response_pending = True
        self._used_order_ids.add(replacement.order_id)
        self._schedule_report(working, ExecutionType.REPLACED, OrderStatus.REPLACED)

        replacement_working = _WorkingOrder(
            order=replacement,
            status=OrderStatus.ACCEPTED,
        )
        self._orders[replacement.order_id] = replacement_working
        self._schedule_report(
            replacement_working,
            ExecutionType.ACKNOWLEDGED,
            OrderStatus.ACCEPTED,
        )
        latest_market = self._latest_markets.get(replacement.symbol)
        if latest_market is not None:
            decision = self._attempt(
                replacement_working, latest_market, LiquidityRole.TAKER
            )
            if decision is None and replacement.time_in_force is TimeInForce.IOC:
                self._schedule_expiry(replacement_working)

    def request_position_snapshot(self, account_id: str) -> None:
        """Validate the protocol command; positions belong to the accounting hub."""
        text(account_id, "account_id")

    def state(self, order_id: str) -> CryptoOrderState:
        """Return an immutable venue-side state snapshot."""
        return self._snapshot(self._working(order_id))

    def order_state(self, order_id: str) -> CryptoOrderState:
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
                LiquidityRole.MAKER,
                available[working.order.side],
            )
            if decision is not None:
                available[working.order.side] -= decision.quantity
            elif working.order.time_in_force is TimeInForce.IOC:
                self._schedule_expiry(working)

    def _attempt(
        self,
        working: _WorkingOrder,
        market: MarketEvent,
        role: LiquidityRole,
        available_quantity: Decimal | None = None,
    ) -> FillDecision | None:
        if working.remaining_quantity <= 0:
            return None
        decision = self._decide(working, market, available_quantity)
        if decision is None:
            return None
        if not isinstance(decision, FillDecision):
            raise AlpacaCryptoVenueError(
                "fill plugin must return a FillDecision or None"
            )
        if decision.liquidity != role.value:
            decision = replace(decision, liquidity=role.value)
        working.last_decision = decision
        self._decisions.append(decision)
        self._schedule_fill(working, decision)
        return decision

    def _decide(
        self,
        working: _WorkingOrder,
        market: MarketEvent,
        available_quantity: Decimal | None,
    ) -> FillDecision | None:
        try:
            return self._fill_model(
                working.order,
                market,
                working.remaining_quantity,
                available_quantity=available_quantity,
                stop_triggered=False,
            )
        except TypeError:
            # A deliberately small third-party plugin may only accept the
            # order, market, and remaining quantity.  It remains deterministic.
            return self._fill_model(working.order, market, working.remaining_quantity)

    def _schedule_fill(self, working: _WorkingOrder, decision: FillDecision) -> None:
        if decision.order_id != working.order.order_id:
            raise AlpacaCryptoVenueError(
                "fill decision order_id does not match working order"
            )
        if decision.quantity > working.remaining_quantity:
            raise AlpacaCryptoVenueError("fill decision exceeds the order remainder")
        timestamp = self._response_timestamp()
        fill_id = self._next_id("fill")
        fill = Fill(
            event_id=fill_id,
            timestamp=timestamp,
            sequence=self._next_sequence(),
            fill_id=fill_id,
            order_id=working.order.order_id,
            symbol=working.order.symbol,
            side=working.order.side,
            quantity=decision.quantity,
            price=decision.price,
            fee=_ZERO,
            liquidity=decision.liquidity,
        )
        fee = self._reconcile_fee(fill)
        fill = replace(fill, fee=fee)
        working.filled_quantity += decision.quantity
        working.filled_notional += decision.quantity * decision.price
        working.fees_paid += fee
        working.last_decision = decision
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
        self._ledger.append(
            CryptoFeeLedgerEntry(
                fill_id=fill_id,
                order_id=working.order.order_id,
                symbol=working.order.symbol,
                side=working.order.side,
                quantity=decision.quantity,
                price=decision.price,
                liquidity=decision.liquidity,
                fee=fee,
            )
        )
        self._schedule_report(
            working,
            execution_type,
            status,
            last_fill_quantity=decision.quantity,
            last_fill_price=decision.price,
        )
        self._bus.schedule(fill)
        if (
            status is OrderStatus.PARTIALLY_FILLED
            and working.order.time_in_force is TimeInForce.IOC
        ):
            self._schedule_expiry(working, allow_pending=True)

    def _reconcile_fee(self, fill: Fill) -> Decimal:
        try:
            role = LiquidityRole(fill.liquidity)
        except (TypeError, ValueError) as error:
            raise AlpacaCryptoVenueError(
                "crypto fills require maker or taker liquidity"
            ) from error
        local_total = sum(
            (component.amount for component in self._fee_model.calculate(fill)),
            start=_ZERO,
        )
        reported = self._fee_schedule.charge(fill.quantity, fill.price, role)
        if local_total != reported:
            raise AlpacaCryptoFeeMismatchError(
                f"fill {fill.fill_id!r} fee mismatch: the configured maker-taker "
                f"model charges {local_total} {self._fee_schedule.currency} but "
                f"the venue fee schedule reports "
                f"{reported} {self._fee_schedule.currency}"
            )
        return local_total

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

    @staticmethod
    def _validate_order(order: SingleLegOrder) -> None:
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be a SingleLegOrder")
        if not is_crypto_pair(order.symbol):
            raise AlpacaCryptoVenueError(
                f"crypto orders must use BASE/QUOTE pairs, not {order.symbol!r}"
            )
        if order.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise AlpacaCryptoVenueError(
                "crypto orders support market and limit types only"
            )

    def _working(self, order_id: str) -> _WorkingOrder:
        try:
            return self._orders[order_id]
        except KeyError as error:
            raise UnknownCryptoOrderError(order_id) from error

    def _snapshot(self, working: _WorkingOrder) -> CryptoOrderState:
        return CryptoOrderState(
            order=working.order,
            status=working.status,
            filled_quantity=working.filled_quantity,
            remaining_quantity=working.remaining_quantity,
            fees_paid=working.fees_paid,
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
            raise AlpacaCryptoVenueError(
                "a timestamped order or market event is required"
            )
        return head + self._latency

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _next_id(self, kind: str) -> str:
        self._ordinal += 1
        return f"{self._venue}-{kind}-{self._ordinal}"


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return value


def _keys(values: Mapping[str, object], expected: set[str], field_name: str) -> None:
    actual = set(values)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ValueError(f"{field_name} fields are invalid: {', '.join(details)}")


def _identifier(value: object, field_name: str = "schedule_id") -> str:
    result = text(value, field_name)
    if _SCHEDULE_ID.fullmatch(result) is None:
        raise ValueError(f"{field_name} contains invalid characters")
    return result


def _currency(value: object) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError("currency must be a three-letter uppercase code")
    return value


def _nonnegative_decimal(value: object, field_name: str = "rate_bps") -> Decimal:
    result = exact_decimal(value, field_name)
    if result < _ZERO:
        raise ValueError(f"{field_name} must not be negative")
    return result


def _decimal_value(value: object, field_name: str = "rounding.precision") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"{field_name} must be an exact decimal")
    return exact_decimal(value, field_name)


def _rounding_mode(value: object) -> RoundingMode:
    if not isinstance(value, str):
        raise TypeError("rounding.mode must be a string")
    return RoundingMode(value)


CryptoVenue = AlpacaCryptoVenue

__all__ = [
    "AlpacaCryptoFeeMismatchError",
    "AlpacaCryptoFeeSchedule",
    "AlpacaCryptoVenue",
    "AlpacaCryptoVenueError",
    "CryptoFeeLedgerEntry",
    "CryptoOrderState",
    "CryptoVenue",
    "DuplicateCryptoOrderError",
    "FillPlugin",
    "MarketEvent",
    "UnknownCryptoOrderError",
    "is_crypto_pair",
]
