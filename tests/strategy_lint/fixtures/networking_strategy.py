"""Failing fixture: performs network I/O from inside a strategy."""

import socket
from urllib.request import urlopen

from harness.strategy.context import StrategyContext
from harness.strategy.contracts import Strategy

FEED_URL = "https://example.invalid/feed"


class NetworkingStrategy(Strategy):
    """Fetch data over the network instead of using ``context.data``."""

    def on_start(self, context: StrategyContext) -> None:
        self._connection = socket.socket()
        self._response = urlopen(FEED_URL)
        context.log.info("connected", url=FEED_URL)
