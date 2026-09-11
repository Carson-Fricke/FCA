"""Mean-reversion reference strategy exercising cancel, replace, and partials."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from harness.core.symbols import Symbol
from harness.core.types import (
    ExecutionReport,
    Fill,
    OrderStatus,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.strategy.contracts import Strategy, StrategyContext


def _utc_close_time(value: datetime | str) -> datetime:
    """Normalize one configured session close into an aware UTC datetime."""
    stamp = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if stamp.tzinfo is None:
        raise ValueError("session close times must be timezone-aware")
    return stamp.astimezone(UTC)


class _Phase(StrEnum):
    """One deterministic episode step for the mean-reversion state machine."""

    IDLE = "idle"
    ENTRY_WORKING = "entry_working"
    ENTRY_CANCELING = "entry_canceling"
    REQUOTE_WORKING = "requote_working"
    REQUOTE_ARMED = "requote_armed"
    REPLACEMENT_WORKING = "replacement_working"
    REPLACEMENT_CANCELING = "replacement_canceling"
    DONE = "done"


@dataclass(slots=True)
class OrderRecord:
    """One strategy order and the latest venue report observed for it."""

    order_id: str
    role: str
    symbol: str
    side: str
    quantity: Decimal
    status: str = "submitted"
    filled_quantity: Decimal = Decimal("0")


INSTANCES: list[MeanReversionStrategy] = []
"""Instances created by loaders, in creation order, for test observability."""


class MeanReversionStrategy(Strategy):
    """Trade toward a reference level with actively managed limit orders.

    Every episode follows one deterministic script: submit a marketable limit
    order toward the reference level, cancel its remainder after the first
    partial fill, rest a requote beyond the touch, replace it with a marketable
    limit, then cancel the replacement remainder after its first partial fill.
    Episodes start only when price deviates from the reference level by at
    least ``deviation_threshold`` and never start inside the pre-close
    liquidation buffer.  All venue interaction uses ``context.orders`` cancel
    and replace commands, so the fixture deterministically exercises
    cancellation, replacement, and partial fills.
    """

    def __init__(
        self,
        *,
        reference_price: str | Decimal = "500.00",
        deviation_threshold: str | Decimal = "0.25",
        entry_quantity: str | int | Decimal = 400,
        resting_offset: str | Decimal = "0.50",
        session_close_times: tuple[datetime | str, ...] = (),
        liquidation_buffer_minutes: int = 30,
        max_episodes: int = 1,
        order_prefix: str = "mr",
    ) -> None:
        self.reference_price = Decimal(str(reference_price))
        self.deviation_threshold = Decimal(str(deviation_threshold))
        self.entry_quantity = Decimal(str(entry_quantity))
        self.resting_offset = Decimal(str(resting_offset))
        if self.deviation_threshold <= 0 or self.entry_quantity <= 0:
            raise ValueError("deviation_threshold and entry_quantity must be positive")
        if self.resting_offset <= 0:
            raise ValueError("resting_offset must be positive")
        self.session_close_times = tuple(
            sorted(_utc_close_time(value) for value in session_close_times)
        )
        self.liquidation_buffer = timedelta(minutes=int(liquidation_buffer_minutes))
        self.max_episodes = int(max_episodes)
        self.order_prefix = str(order_prefix)
        self.context: StrategyContext | None = None
        self.episodes = 0
        self.cancellation_count = 0
        self.replacement_count = 0
        self.partial_fill_count = 0
        self.fill_count = 0
        self.ledger: dict[str, OrderRecord] = {}
        self._phase = _Phase.IDLE
        self._universe: frozenset[str] = frozenset()
        self._last_quotes: dict[str, QuoteEvent] = {}
        self._entry_id: str | None = None
        self._requote_id: str | None = None
        self._replacement_id: str | None = None
        self._requote_side = Side.SELL
        self._requote_quantity = Decimal("0")
        self._ordinal = 0
        INSTANCES.append(self)

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._universe = frozenset(str(symbol) for symbol in context.universe.symbols)

    def on_quote(self, event: QuoteEvent) -> None:
        context = self.context
        if context is None:
            return
        symbol = str(event.symbol)
        if self._universe and symbol not in self._universe:
            return
        self._last_quotes[symbol] = event
        if self._phase is _Phase.REQUOTE_ARMED and self._requote_id is not None:
            self._replace_requote(symbol, event)
            return
        if self._phase is not _Phase.IDLE or self.episodes >= self.max_episodes:
            return
        now = context.clock.now()
        close = self._next_close(now)
        if close is None or now >= close - self.liquidation_buffer:
            return
        mid = (event.bid_price + event.ask_price) / 2
        deviation = mid - self.reference_price
        if deviation >= self.deviation_threshold:
            side = Side.SELL
            touch = event.bid_price
        elif deviation <= -self.deviation_threshold:
            side = Side.BUY
            touch = event.ask_price
        else:
            return
        self._submit_entry(symbol, side, touch, event.sequence)

    def on_execution_report(self, event: ExecutionReport) -> None:
        context = self.context
        if context is None:
            return
        record = self.ledger.get(event.order_id)
        if record is None:
            return
        record.status = event.status.value
        record.filled_quantity = event.filled_quantity
        if event.status is OrderStatus.PARTIALLY_FILLED:
            self.partial_fill_count += 1
        elif event.status is OrderStatus.CANCELED:
            self.cancellation_count += 1
        elif event.status is OrderStatus.REPLACED:
            self.replacement_count += 1
        self._advance(event)

    def on_fill(self, event: Fill) -> None:
        context = self.context
        if context is None or not context.orders.owns(event.order_id):
            return
        self.fill_count += 1

    def on_stop(self, context: StrategyContext) -> None:
        context.emit(
            "mean_reversion_summary",
            phase=self._phase.value,
            episodes=self.episodes,
            cancellations=self.cancellation_count,
            replacements=self.replacement_count,
        )

    def _advance(self, event: ExecutionReport) -> None:
        context = self.context
        assert context is not None
        order_id = event.order_id
        if self._phase is _Phase.ENTRY_WORKING and order_id == self._entry_id:
            if event.status is OrderStatus.PARTIALLY_FILLED:
                context.orders.cancel(order_id)
                self._phase = _Phase.ENTRY_CANCELING
            elif event.status is OrderStatus.FILLED:
                self._finish_episode()
        elif (
            self._phase is _Phase.ENTRY_CANCELING
            and order_id == self._entry_id
            and event.status is OrderStatus.CANCELED
        ):
            self._submit_requote(event)
        elif (
            self._phase is _Phase.REQUOTE_WORKING
            and order_id == self._requote_id
            and event.status is OrderStatus.ACCEPTED
        ):
            self._phase = _Phase.REQUOTE_ARMED
        elif (
            self._phase is _Phase.REQUOTE_ARMED
            and order_id == self._requote_id
            and event.status is OrderStatus.FILLED
        ):
            self._finish_episode()
        elif self._phase is _Phase.REPLACEMENT_WORKING and (
            order_id == self._replacement_id
        ):
            if event.status is OrderStatus.PARTIALLY_FILLED:
                context.orders.cancel(order_id)
                self._phase = _Phase.REPLACEMENT_CANCELING
            elif event.status is OrderStatus.FILLED:
                self._finish_episode()
        elif (
            self._phase is _Phase.REPLACEMENT_CANCELING
            and order_id == self._replacement_id
            and event.status is OrderStatus.CANCELED
        ):
            self._finish_episode()

    def _submit_entry(
        self, symbol: str, side: Side, touch: Decimal, sequence: int
    ) -> None:
        context = self.context
        assert context is not None
        order = self._build_order(
            symbol, side, self.entry_quantity, touch, sequence, "entry"
        )
        self._entry_id = order.order_id
        self._phase = _Phase.ENTRY_WORKING
        context.orders.submit(order)

    def _submit_requote(self, cancel_report: ExecutionReport) -> None:
        context = self.context
        assert context is not None
        remaining = cancel_report.remaining_quantity
        entry_id = self._entry_id
        if remaining <= 0 or entry_id is None:
            self._finish_episode()
            return
        symbol = self.ledger[entry_id].symbol
        quote = self._last_quotes.get(symbol)
        if quote is None:
            self._finish_episode()
            return
        side = Side(self.ledger[entry_id].side)
        if side is Side.SELL:
            limit = quote.bid_price + self.resting_offset
        else:
            limit = quote.ask_price - self.resting_offset
        order = self._build_order(
            symbol,
            side,
            remaining,
            limit,
            cancel_report.sequence + 1,
            "requote",
        )
        self._requote_id = order.order_id
        self._requote_side = side
        self._requote_quantity = remaining
        self._phase = _Phase.REQUOTE_WORKING
        context.orders.submit(order)

    def _replace_requote(self, symbol: str, quote: QuoteEvent) -> None:
        context = self.context
        assert context is not None
        requote_id = self._requote_id
        if requote_id is None:
            return
        limit = quote.bid_price if self._requote_side is Side.SELL else quote.ask_price
        replacement = self._build_order(
            symbol,
            self._requote_side,
            self._requote_quantity,
            limit,
            quote.sequence + 1,
            "replacement",
        )
        self._replacement_id = replacement.order_id
        self._phase = _Phase.REPLACEMENT_WORKING
        context.orders.replace(requote_id, replacement)

    def _build_order(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        limit: Decimal,
        sequence: int,
        role: str,
    ) -> SingleLegOrder:
        context = self.context
        assert context is not None
        self._ordinal += 1
        order_id = f"{self.order_prefix}-{self._ordinal:04d}"
        order = SingleLegOrder(
            event_id=order_id,
            timestamp=context.clock.now(),
            sequence=sequence,
            order_id=order_id,
            symbol=Symbol(symbol),
            side=side,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit,
        )
        self.ledger[order_id] = OrderRecord(
            order_id=order_id,
            role=role,
            symbol=symbol,
            side=side.value,
            quantity=quantity,
        )
        return order

    def _finish_episode(self) -> None:
        self.episodes += 1
        self._entry_id = None
        self._requote_id = None
        self._replacement_id = None
        self._phase = _Phase.IDLE if self.episodes < self.max_episodes else _Phase.DONE

    def _next_close(self, now: datetime) -> datetime | None:
        for close in self.session_close_times:
            if close > now:
                return close
        return None


__all__ = ["MeanReversionStrategy", "OrderRecord"]
