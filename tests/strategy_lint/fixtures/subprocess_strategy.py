"""Failing fixture: starts other processes from inside a strategy."""

from subprocess import run

from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class SubprocessStrategy(Strategy):
    """Shell out instead of using the injected venue boundary."""

    def on_start(self, context: StrategyContext) -> None:
        self._completed = run(["echo", "started"], check=True)
        context.log.info("started")
