"""Small callback-recording strategy used as a public contract fixture."""

from __future__ import annotations

from collections.abc import Mapping

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
from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class ReferenceStrategy(Strategy):
    """Record every callback in the contract's documented dispatch order."""

    def __init__(self, **parameters: object) -> None:
        self.parameters: Mapping[str, object] = dict(parameters)
        self.callbacks: list[str] = []
        self.events: list[object] = []

    def on_start(self, context: StrategyContext) -> None:
        self.callbacks.append("on_start")

    def on_quote(self, event: QuoteEvent) -> None:
        self.callbacks.append("on_quote")
        self.events.append(event)

    def on_trade(self, event: TradeEvent) -> None:
        self.callbacks.append("on_trade")
        self.events.append(event)

    def on_order(self, event: SingleLegOrder | MultiLegOrder) -> None:
        self.callbacks.append("on_order")
        self.events.append(event)

    def on_execution_report(self, event: ExecutionReport) -> None:
        self.callbacks.append("on_execution_report")
        self.events.append(event)

    def on_fill(self, event: Fill) -> None:
        self.callbacks.append("on_fill")
        self.events.append(event)

    def on_position(self, event: Position) -> None:
        self.callbacks.append("on_position")
        self.events.append(event)

    def on_book(self, event: Book) -> None:
        self.callbacks.append("on_book")
        self.events.append(event)

    def on_account(self, event: AccountSnapshot) -> None:
        self.callbacks.append("on_account")
        self.events.append(event)

    def on_stop(self, context: StrategyContext) -> None:
        self.callbacks.append("on_stop")


__all__ = ["ReferenceStrategy"]
