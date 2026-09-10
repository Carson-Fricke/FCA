"""Tick-native incremental indicators for the quant harness.

Every indicator in this package consumes kernel-dispatched ``QuoteEvent`` and
``TradeEvent`` values in event time, keeps O(1)-amortized incremental state,
and emits values at deterministic half-open bucket boundaries derived exactly
like the kernel's bar derivation.  There is no wall-clock access, no window
recomputation, and no external data source, so backtest replay, paper, and
live replays of the same event ordering produce identical outputs.  Declarative
consumers request indicators by name through :func:`create_indicator`.
"""

from harness.indicators.base import (
    ARITHMETIC_CONTEXT,
    ARITHMETIC_PRECISION,
    BASKET_SNAPSHOT_SCHEMA,
    MIDNIGHT_UTC,
    SNAPSHOT_SCHEMA,
    IndicatorConfigurationError,
    IndicatorOrderingError,
    IndicatorRouter,
    IndicatorSample,
    IndicatorSnapshotError,
    TickIndicator,
    bucket_start,
)
from harness.indicators.momentum import RelativeStrengthIndex
from harness.indicators.overlays import (
    ExponentialMovingAverage,
    MidSimpleMovingAverage,
    SimpleMovingAverage,
)
from harness.indicators.registry import (
    DEFAULT_REGISTRY,
    IndicatorParam,
    IndicatorParamKind,
    IndicatorRegistry,
    IndicatorSpec,
    create_indicator,
    indicator_names,
    indicator_spec,
)
from harness.indicators.volatility import AverageTrueRange, RealizedVolatility
from harness.indicators.volume import (
    DollarVolume,
    OnBalanceVolume,
    VolumeWeightedAveragePrice,
)

__all__ = [
    "ARITHMETIC_CONTEXT",
    "ARITHMETIC_PRECISION",
    "AverageTrueRange",
    "BASKET_SNAPSHOT_SCHEMA",
    "DEFAULT_REGISTRY",
    "DollarVolume",
    "ExponentialMovingAverage",
    "IndicatorConfigurationError",
    "IndicatorOrderingError",
    "IndicatorParam",
    "IndicatorParamKind",
    "IndicatorRegistry",
    "IndicatorRouter",
    "IndicatorSample",
    "IndicatorSnapshotError",
    "IndicatorSpec",
    "MIDNIGHT_UTC",
    "MidSimpleMovingAverage",
    "OnBalanceVolume",
    "SNAPSHOT_SCHEMA",
    "RealizedVolatility",
    "RelativeStrengthIndex",
    "SimpleMovingAverage",
    "TickIndicator",
    "VolumeWeightedAveragePrice",
    "bucket_start",
    "create_indicator",
    "indicator_names",
    "indicator_spec",
]
