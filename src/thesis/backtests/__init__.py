"""Discover retained executions and import published backtest evidence.

The harness MCP tool loop launches and reads backtests; this package turns a
finished harness job into the same
:class:`~thesis.backtests.records.BacktestRecord` the HTTP backtest routes read, so
a job performed from chat appears on the backtests page and renders inline.
:func:`import_backtest_run` is the pure artifact-to-record mapper and
:class:`BacktestRunCoordinator` is the tool-result observer that imports a
terminal job and persists the record under its allocated ``job_id``.
The observational catalog lists executions independently of report availability.
"""

from thesis.backtests.coordinator import BacktestRunCoordinator
from thesis.backtests.run_import import (
    BacktestRunError,
    BacktestRunPending,
    import_backtest_run,
)

__all__ = [
    "BacktestRunCoordinator",
    "BacktestRunError",
    "BacktestRunPending",
    "import_backtest_run",
]
