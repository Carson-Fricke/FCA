"""Buy-and-hold reference strategy shipped with the harness examples."""

from __future__ import annotations

from decimal import Decimal

from harness.core.symbols import Symbol
from harness.core.types import (
    Fill,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class BuyAndHoldStrategy(Strategy):
    """Enter one long market position per configured symbol and hold it.

    The strategy submits exactly one market order per universe symbol on its
    first quote early in the run and never exits, so every entered position is
    still held at the end of the replay.  All state is derived from context
    capabilities, so identical inputs always produce identical orders.  The
    entered symbols and the order-id ordinal are exported into the context
    ``StateStore`` on every change, so a snapshot restore rehydrates them and
    a restarted session never re-enters a position the captured books hold.
    """

    def __init__(
        self,
        *,
        quantity: str | int | Decimal = 25,
        order_prefix: str = "bh",
    ) -> None:
        self.quantity = Decimal(str(quantity))
        self.order_prefix = str(order_prefix)
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        self.context: StrategyContext | None = None
        self.entered_symbols: tuple[str, ...] = ()
        self.submitted_order_ids: tuple[str, ...] = ()
        self._universe: frozenset[str] = frozenset()
        self._order_ids: list[str] = []
        self._ordinal = 0

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._universe = frozenset(str(symbol) for symbol in context.universe.symbols)
        self._rehydrate()

    def on_quote(self, event: QuoteEvent) -> None:
        context = self.context
        if context is None:
            return
        symbol = str(event.symbol)
        if self._universe and symbol not in self._universe:
            return
        self._rehydrate()
        if symbol in self.entered_symbols:
            return
        self.entered_symbols = tuple(sorted((*self.entered_symbols, symbol)))
        context.state["entered_symbols"] = self.entered_symbols
        order = self._order(symbol, event.sequence)
        self._order_ids.append(order.order_id)
        self.submitted_order_ids = tuple(self._order_ids)
        context.state["order_ordinal"] = self._ordinal
        context.orders.submit(order)
        context.emit(
            "buy_and_hold_entry",
            symbol=symbol,
            quantity=str(self.quantity),
        )

    def on_fill(self, event: Fill) -> None:
        context = self.context
        if context is None or not context.orders.owns(event.order_id):
            return
        context.emit(
            "buy_and_hold_fill",
            order_id=event.order_id,
            symbol=str(event.symbol),
            quantity=str(event.quantity),
        )

    def on_stop(self, context: StrategyContext) -> None:
        context.emit("buy_and_hold_summary", symbols=list(self.entered_symbols))

    def _rehydrate(self) -> None:
        """Re-read the exported state after a snapshot restore replays it.

        A restored shell imports the captured ``StateStore`` values into the
        context after the binding restart, so every callback re-syncs from
        the store before making entry decisions.
        """
        context = self.context
        if context is None:
            return
        stored = context.state.get("entered_symbols", ())
        if isinstance(stored, (list, tuple, set, frozenset)):
            self.entered_symbols = tuple(sorted(str(symbol) for symbol in stored))
        ordinal = context.state.get("order_ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            self._ordinal = max(self._ordinal, ordinal)

    def _order(self, symbol: str, sequence: int) -> SingleLegOrder:
        context = self.context
        assert context is not None
        self._ordinal += 1
        order_id = f"{self.order_prefix}-{self._ordinal:04d}"
        return SingleLegOrder(
            event_id=order_id,
            timestamp=context.clock.now(),
            sequence=sequence,
            order_id=order_id,
            symbol=Symbol(symbol),
            side=Side.BUY,
            quantity=self.quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
        )


__all__ = ["BuyAndHoldStrategy"]
