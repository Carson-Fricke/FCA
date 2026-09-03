"""Failing fixture: runs work concurrently outside the scheduler."""

import threading
from concurrent.futures import ThreadPoolExecutor

from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class ConcurrencyStrategy(Strategy):
    """Spawn threads whose ordering the kernel cannot replay."""

    def on_start(self, context: StrategyContext) -> None:
        self._worker = threading.Thread(target=self._poll)
        self._pool = ThreadPoolExecutor(max_workers=1)
        context.log.info("workers started")

    def _poll(self) -> None:
        self._worker = None
