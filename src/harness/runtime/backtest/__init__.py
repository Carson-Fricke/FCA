"""Deterministic backtest runner and reproducibility manifest.

The runner composes the existing harness capabilities into one reproducible
execution: local data preflight, :class:`~harness.data.replay.LakeReplayFeed`
replay, a :class:`~harness.core.clock.SimClock`-driven
:class:`~harness.core.events.EventBus`, strategy binding through
:mod:`harness.strategy`, a :class:`~harness.venues.sim.SimulatedVenue` with a
pessimistic fill plugin, fee application through :mod:`harness.fills`,
portfolio accounting through :class:`~harness.portfolio.hub.PortfolioHub`, pre
trade risk through :class:`~harness.risk.pipeline.RiskPipeline`, and an
optional :class:`~harness.data.history.HistoricalView` look-ahead guard.

Every run returns a :class:`BacktestManifest` carrying each reproducibility
input and a :class:`BacktestResult` whose artifacts are byte-identical across
repeated execution.  The package never reads wall time, consumes unseeded
randomness, opens a network connection, or writes outside an injected path.

The public surface below is re-exported unchanged from the focused submodules
(``errors``, ``fills``, ``fees``, ``fixture``, ``preflight``, ``manifest``,
``strategies``, and ``engine``), so ``harness.runtime.backtest`` keeps its
exact prior import surface.
"""

from harness.runtime.backtest.engine import (
    BacktestOptions,
    build_manifest,
    run_backtest,
)
from harness.runtime.backtest.errors import (
    AbortPolicy,
    BacktestAbort,
    BacktestAborted,
    BacktestConfigurationError,
    BacktestError,
    BacktestStatus,
    DataPreflightError,
    FillModel,
    LookaheadViolationError,
    StrategyExecutionError,
    StrategyHalt,
    StrategyHaltPolicy,
    StrategyLoadError,
    TeardownPolicy,
)
from harness.runtime.backtest.fees import fee_schedule_from_config
from harness.runtime.backtest.fills import (
    FillModelOptions,
    FillPlugin,
    build_fill_model,
    fill_model_identity,
    fill_model_latency,
)
from harness.runtime.backtest.fixture import (
    DataPreflight,
    ReplayFixture,
    SyntheticDataOptions,
    backtest_sessions,
    local_lake_fixture,
    synthetic_fixture,
    write_synthetic_lake,
)
from harness.runtime.backtest.manifest import (
    MANIFEST_VERSION,
    BacktestManifest,
    BacktestResult,
    StrategyIdentity,
    code_fingerprint,
)
from harness.runtime.backtest.preflight import (
    MarketDataSource,
    preflight_data,
    resolve_data_source,
)
from harness.runtime.backtest.strategies import (
    StrategyLoader,
    default_strategy_loader,
    load_strategy_class,
)

__all__ = [
    "AbortPolicy",
    "BacktestAbort",
    "BacktestAborted",
    "BacktestConfigurationError",
    "BacktestError",
    "BacktestManifest",
    "BacktestOptions",
    "BacktestResult",
    "BacktestStatus",
    "DataPreflight",
    "DataPreflightError",
    "FillModel",
    "FillModelOptions",
    "FillPlugin",
    "LookaheadViolationError",
    "MANIFEST_VERSION",
    "MarketDataSource",
    "ReplayFixture",
    "StrategyExecutionError",
    "StrategyHalt",
    "StrategyHaltPolicy",
    "StrategyIdentity",
    "StrategyLoadError",
    "StrategyLoader",
    "SyntheticDataOptions",
    "TeardownPolicy",
    "backtest_sessions",
    "build_fill_model",
    "build_manifest",
    "code_fingerprint",
    "default_strategy_loader",
    "fee_schedule_from_config",
    "fill_model_identity",
    "fill_model_latency",
    "load_strategy_class",
    "local_lake_fixture",
    "preflight_data",
    "resolve_data_source",
    "run_backtest",
    "synthetic_fixture",
    "write_synthetic_lake",
]
