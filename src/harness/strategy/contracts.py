"""Public strategy callback contract and its documented event ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from harness.core.types import (
    AccountSnapshot,
    Book,
    ExecutionReport,
    Fill,
    MultiLegOrder,
    Position,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)

if TYPE_CHECKING:
    from harness.strategy.context import StrategyContext


CALLBACK_ORDER = (
    "on_start",
    "on_quote",
    "on_trade",
    "on_order",
    "on_execution_report",
    "on_fill",
    "on_position",
    "on_book",
    "on_account",
    "on_stop",
)


class Strategy:
    """Base class for one mode-independent strategy implementation.

    Event callbacks receive the immutable event only. ``on_start`` supplies the
    constrained context, which strategies commonly retain as a private field.
    The same implementation can therefore be attached to replay, paper, or
    live adapters without changing strategy code.
    """

    callback_order: ClassVar[tuple[str, ...]] = CALLBACK_ORDER

    def on_start(self, context: StrategyContext) -> None:
        """Initialize strategy state before the first event."""

    def on_quote(self, event: QuoteEvent) -> None:
        """Handle a subscribed quote."""

    def on_trade(self, event: TradeEvent) -> None:
        """Handle a subscribed trade."""

    def on_order(self, event: SingleLegOrder | MultiLegOrder) -> None:
        """Handle a subscribed order event."""

    def on_execution_report(self, event: ExecutionReport) -> None:
        """Handle a subscribed execution report."""

    def on_fill(self, event: Fill) -> None:
        """Handle a subscribed fill after portfolio mutation."""

    def on_position(self, event: Position) -> None:
        """Handle a subscribed position snapshot."""

    def on_book(self, event: Book) -> None:
        """Handle a subscribed market-book event."""

    def on_account(self, event: AccountSnapshot) -> None:
        """Handle a subscribed account snapshot."""

    def on_stop(self, context: StrategyContext) -> None:
        """Finalize strategy state after the final event."""


__all__ = ["CALLBACK_ORDER", "Strategy"]
