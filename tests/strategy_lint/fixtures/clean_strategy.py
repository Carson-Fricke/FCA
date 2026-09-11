"""A strategy that stays inside the injected context boundaries.

This fixture is the positive control for the sandbox lint: it builds timestamps
explicitly, draws jitter from a seeded generator, and reaches the engine only
through ``StrategyContext``.
"""

from datetime import UTC, datetime
from decimal import Decimal
from random import Random

from harness.core.types import QuoteEvent
from harness.strategy.contracts import Strategy, StrategyContext

SESSION_OPEN = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


class CleanStrategy(Strategy):
    """Record wide quote spreads using only context boundaries."""

    def __init__(self, threshold: Decimal = Decimal("0.01")) -> None:
        self.threshold = threshold
        self._jitter = Random(1729)
        self.observations: list[str] = []

    def on_start(self, context: StrategyContext) -> None:
        context.state["threshold"] = self.threshold
        context.state["session_open"] = SESSION_OPEN
        context.log.info("clean strategy started", threshold=str(self.threshold))
        context.emit("started", threshold=str(self.threshold))

    def on_quote(self, event: QuoteEvent) -> None:
        spread = event.ask_price - event.bid_price
        jitter = Decimal(self._jitter.randint(0, 2)) / Decimal(1000)
        if spread + jitter >= self.threshold:
            self.observations.append(str(event.symbol))

    def on_stop(self, context: StrategyContext) -> None:
        context.emit("stopped", observations=len(self.observations))
