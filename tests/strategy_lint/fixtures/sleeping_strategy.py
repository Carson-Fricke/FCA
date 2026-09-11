"""Failing fixture: sleeps instead of scheduling timestamped events."""

import time

from harness.strategy.contracts import Strategy, StrategyContext


class SleepingStrategy(Strategy):
    """Block the deterministic event loop by sleeping."""

    def on_start(self, context: StrategyContext) -> None:
        time.sleep(0.5)
        context.log.info("slept")
