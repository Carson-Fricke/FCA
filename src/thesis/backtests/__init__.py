"""Import completed harness backtest runs into the workspace backtest store.

The harness MCP tool loop launches and reads backtests; this package turns a
finished harness run into the same :class:`~thesis.http.backtests.BacktestRecord`
the HTTP backtest routes read, so a run performed from chat appears on the
backtests page and renders inline.  :func:`import_backtest_run` is the pure
artifact-to-record mapper and :class:`BacktestRunCoordinator` is the
tool-result observer that polls a launch to completion and persists the record.
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
