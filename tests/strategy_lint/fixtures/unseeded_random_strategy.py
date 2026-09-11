"""Failing fixture: draws unseeded randomness instead of ``context.random``."""

import os
import random
import uuid

from harness.strategy.contracts import Strategy, StrategyContext


class UnseededRandomStrategy(Strategy):
    """Use process-global entropy that replay cannot reproduce."""

    def on_start(self, context: StrategyContext) -> None:
        self._tag = uuid.uuid4()
        self._salt = os.urandom(8)
        self._size = random.randint(1, 10)
        self._draw = random.random()
        random.seed(7)
        context.log.info("tagged", tag=str(self._tag))
