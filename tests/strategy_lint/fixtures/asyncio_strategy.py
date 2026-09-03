"""Failing fixture: drives an asyncio event loop inside the kernel."""

import asyncio

from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy


class AsyncioStrategy(Strategy):
    """Await work instead of scheduling it through ``context.scheduler``."""

    async def _bootstrap(self) -> None:
        await asyncio.sleep(0.1)

    def on_start(self, context: StrategyContext) -> None:
        context.log.info("scheduled bootstrap")
