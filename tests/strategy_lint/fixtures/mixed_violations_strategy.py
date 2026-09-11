"""Failing fixture: combines wall-clock, sleeping, and networking violations."""

import socket
import time

from harness.strategy.contracts import Strategy, StrategyContext


class MixedViolationStrategy(Strategy):
    """Break several sandbox rules in a single strategy."""

    def on_start(self, context: StrategyContext) -> None:
        self._connection = socket.socket()
        time.sleep(0.1)
        self._stamp = time.time()
        context.log.info("started")
