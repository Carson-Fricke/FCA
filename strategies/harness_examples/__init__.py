"""Reference strategies built against the public harness contract."""

from strategies.harness_examples.buy_and_hold import BuyAndHoldStrategy
from strategies.harness_examples.intraday_momentum import IntradayMomentumStrategy
from strategies.harness_examples.mean_reversion import MeanReversionStrategy
from strategies.harness_examples.short_put_spread import ShortPutSpreadStrategy

__all__ = [
    "BuyAndHoldStrategy",
    "IntradayMomentumStrategy",
    "MeanReversionStrategy",
    "ShortPutSpreadStrategy",
]
