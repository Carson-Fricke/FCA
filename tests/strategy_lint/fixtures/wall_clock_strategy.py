"""Failing fixture: reads the wall clock instead of ``context.clock``."""

import time
from datetime import datetime

from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class WallClockStrategy(Strategy):
    """Stamp decisions with wall-clock time instead of engine time."""

    def __init__(self) -> None:
        self.started_at = datetime.now()

    def on_start(self, context: StrategyContext) -> None:
        self.started_at = datetime.now()
        context.log.info("started", at=time.time())
