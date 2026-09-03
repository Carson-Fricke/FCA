"""Backtest error taxonomy and abort, halt, teardown, and status policies.

Shared exact-arithmetic constants for the backtest package also live here so
every higher-level submodule can import them without cycles.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

_ZERO: Final = Decimal("0")
_ONE_DAY: Final = timedelta(days=1)
_MICROSECOND: Final = timedelta(microseconds=1)


# --------------------------------------------------------------------------
# Error taxonomy
# --------------------------------------------------------------------------


class BacktestError(RuntimeError):
    """Base class for every deterministic backtest runner error."""


class BacktestConfigurationError(BacktestError, ValueError):
    """Raised when the supplied configuration cannot drive a backtest."""


class DataPreflightError(BacktestError, ValueError):
    """Raised when local market data fails runnability assurance."""


class StrategyLoadError(BacktestError, ValueError):
    """Raised when a configured strategy implementation cannot be loaded."""


class StrategyExecutionError(BacktestError):
    """Raised when a strategy raises an unexpected exception during a run."""


class LookaheadViolationError(BacktestError):
    """Raised when a strategy reads data newer than the engine clock head."""


class BacktestAborted(BacktestError):
    """Raised by the runner when an aborted run propagates its error."""


class StrategyHalt(BacktestError):
    """Raised by a strategy to request that only that strategy stops."""


class BacktestAbort(BacktestError):
    """Raised by a strategy to request that the whole run stops."""


class _LoopStop(Exception):
    """Internal sentinel used to leave the event loop after teardown."""


# --------------------------------------------------------------------------
# Policies and status
# --------------------------------------------------------------------------


class AbortPolicy(StrEnum):
    """What the runner does once a run has been aborted."""

    RAISE = "raise"
    STOP = "stop"


class StrategyHaltPolicy(StrEnum):
    """What the runner does when one strategy requests a halt."""

    HALT_STRATEGY = "halt_strategy"
    ABORT_RUN = "abort_run"
    RAISE = "raise"


class TeardownPolicy(StrEnum):
    """When the runner invokes strategy ``on_stop`` callbacks."""

    ALWAYS = "always"
    ON_SUCCESS = "on_success"
    NEVER = "never"


class BacktestStatus(StrEnum):
    """Terminal status of one backtest run."""

    COMPLETED = "completed"
    ABORTED = "aborted"


class FillModel(StrEnum):
    """Pessimistic fill plugins selectable by name."""

    SIMPLE_TOUCH = "simple_touch"
    LATENCY_SIZE = "latency_size"
    TRADE_THROUGH = "trade_through"
