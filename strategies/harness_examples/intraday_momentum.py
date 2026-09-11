"""Intraday momentum reference strategy that is flat at every session close."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types import (
    Fill,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
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


class IntradayMomentumStrategy(Strategy):
    """Trade recent trade/quote momentum directionally inside each session.

    Momentum is the exact change across the last ``lookback`` quote mid or
    trade print observations per symbol.  The strategy enters flat positions in
    the momentum direction once the change crosses ``threshold`` and liquidates
    every open position before each configured session close, so the book is
    flat at every session close and never carries a position overnight.  All
    decisions read time through ``context.clock`` and stay deterministic for
    identical inputs.
    """

    def __init__(
        self,
        *,
        lookback: int = 4,
        threshold: str | Decimal = "0.10",
        quantity: str | int | Decimal = 20,
        session_close_times: tuple[datetime | str, ...] = (),
        liquidation_buffer_minutes: int = 10,
        order_prefix: str = "mom",
    ) -> None:
        self.lookback = int(lookback)
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")
        self.threshold = Decimal(str(threshold))
        self.quantity = Decimal(str(quantity))
        if self.threshold <= 0 or self.quantity <= 0:
            raise ValueError("threshold and quantity must be positive")
        self.liquidation_buffer = timedelta(minutes=int(liquidation_buffer_minutes))
        if self.liquidation_buffer < timedelta(0):
            raise ValueError("liquidation_buffer_minutes must not be negative")
        self.session_close_times = tuple(
            sorted(_utc_close_time(value) for value in session_close_times)
        )
        self.order_prefix = str(order_prefix)
        self.context: StrategyContext | None = None
        self._windows: dict[str, deque[Decimal]] = {}
        self._positions: dict[str, Decimal] = {}
        self._pending: set[str] = set()
        self._ordinal = 0

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        for symbol in context.universe.symbols:
            self._windows[str(symbol)] = deque(maxlen=self.lookback)
            self._positions[str(symbol)] = Decimal("0")

    def on_quote(self, event: QuoteEvent) -> None:
        symbol = str(event.symbol)
        self._observe_price(symbol, (event.bid_price + event.ask_price) / 2)
        self._act(symbol, event.sequence)

    def on_trade(self, event: TradeEvent) -> None:
        self._observe_price(str(event.symbol), event.price)

    def on_fill(self, event: Fill) -> None:
        context = self.context
        if context is None or not context.orders.owns(event.order_id):
            return
        symbol = str(event.symbol)
        signed = event.quantity if event.side is Side.BUY else -event.quantity
        position = self._positions.get(symbol, Decimal("0")) + signed
        self._positions[symbol] = position
        self._pending.discard(symbol)

    def on_stop(self, context: StrategyContext) -> None:
        context.emit(
            "momentum_summary",
            positions={
                symbol: str(quantity) for symbol, quantity in self._positions.items()
            },
        )

    def _observe_price(self, symbol: str, price: Decimal) -> None:
        window = self._windows.get(symbol)
        if window is not None:
            window.append(price)

    def _act(self, symbol: str, sequence: int) -> None:
        context = self.context
        if context is None or symbol in self._pending:
            return
        window = self._windows.get(symbol)
        if window is None or len(window) < self.lookback:
            return
        now = context.clock.now()
        close = self._next_close(now)
        if close is None:
            return
        position = self._positions.get(symbol, Decimal("0"))
        if now >= close - self.liquidation_buffer:
            if position != 0:
                self._submit(
                    symbol,
                    _opposite_side(position),
                    abs(position),
                    sequence,
                    "momentum_liquidation",
                )
            return
        if position != 0:
            return
        momentum = window[-1] - window[0]
        if momentum >= self.threshold:
            self._submit(symbol, Side.BUY, self.quantity, sequence, "momentum_entry")
        elif momentum <= -self.threshold:
            self._submit(symbol, Side.SELL, self.quantity, sequence, "momentum_entry")

    def _submit(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        sequence: int,
        signal: str,
    ) -> None:
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
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
        )
        self._pending.add(symbol)
        context.orders.submit(order)
        context.emit(signal, symbol=symbol, side=side.value, quantity=str(quantity))

    def _next_close(self, now: datetime) -> datetime | None:
        for close in self.session_close_times:
            if close > now:
                return close
        return None


def _opposite_side(position: Decimal) -> Side:
    return Side.SELL if position > 0 else Side.BUY


__all__ = ["IntradayMomentumStrategy"]
