"""Canonical workspace backtest report records and their persistence port."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BacktestRecord:
    """One workspace backtest row keyed by its harness job.

    ``job_id`` is the single execution identity allocated at submission and
    reused unchanged by tracking, artifacts, and reports.  A statistic the
    harness did not publish is ``None``; the record never carries a zero in its
    place.
    """

    job_id: str
    workspace_id: str
    strategy_id: str
    name: str
    period_start: str
    period_end: str
    created_at: str
    initial_capital: float | None
    final_capital: float | None
    total_return: float | None
    sharpe_per_sample: float | None
    max_drawdown: float | None
    win_rate: float | None
    trades: int | None
    split: Mapping[str, object] | None = None
    manifest: Mapping[str, object] = field(default_factory=dict)
    equity: tuple[Mapping[str, float | int], ...] = ()
    trade_log: tuple[Mapping[str, object], ...] = ()
    stats: Mapping[str, object] = field(default_factory=dict)
    status: str = "succeeded"
    bundle_ref: str | None = None
    config_path: str | None = None
    chat_id: str | None = None
    execution_id: str | None = None

    @property
    def split_declared(self) -> bool:
        return self.split is not None

    @property
    def capital_change(self) -> float | None:
        """Final minus initial capital, or ``None`` until both ends are published."""
        if self.initial_capital is None or self.final_capital is None:
            return None
        return round(self.final_capital - self.initial_capital, 2)

    def summary_row(self) -> dict[str, object]:
        """Return the BacktestSummaryRow projection."""
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "strategy_id": self.strategy_id,
            "name": self.name,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "capital": self.initial_capital,
            "total_return": self.total_return,
            "sharpe_per_sample": self.sharpe_per_sample,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "trades": self.trades,
            "split_declared": self.split is not None,
            "created_at": self.created_at,
            "chat_id": self.chat_id,
            "execution_id": self.execution_id,
        }

    def _stats_document(self) -> dict[str, object]:
        raw = self.manifest.get("stats")
        if isinstance(raw, Mapping):
            return dict(raw)
        return dict(self._default_stats())

    def detail(self) -> dict[str, object]:
        """Return the BacktestDetail projection."""
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "strategy_id": self.strategy_id,
            "name": self.name,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "capital_change": self.capital_change,
            "equity": [dict(point) for point in self.equity],
            "trades": [dict(trade) for trade in self.trade_log],
            "stats": self._stats_document(),
            "split": dict(self.split) if self.split is not None else None,
            "manifest": dict(self.manifest),
            "status": self.status,
            "bundle_ref": self.bundle_ref,
            "config_path": self.config_path,
            "chat_id": self.chat_id,
            "execution_id": self.execution_id,
        }

    def _default_stats(self) -> dict[str, object]:
        return {
            "total_return": self.total_return,
            "sharpe_per_sample": self.sharpe_per_sample,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "trade_count": self.trades,
        }


class BacktestStore(Protocol):
    """Read boundary over the locally persisted backtest records."""

    def list(self, workspace_id: str) -> tuple[BacktestRecord, ...]:
        """Return every record for the workspace, newest first."""
        ...

    def get(self, workspace_id: str, job_id: str) -> BacktestRecord | None:
        """Return one record by its direct ``(workspace_id, job_id)`` key."""
        ...

    def add(self, record: BacktestRecord) -> BacktestRecord:
        """Atomically persist and return one record.

        Materialization is add-if-absent: a repeated or concurrent import for
        the same ``(workspace_id, job_id)`` returns the existing projection
        instead of creating another one.
        """
        ...


class InMemoryBacktestStore:
    """Local in-memory backtest store with atomic add-if-absent semantics."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, BacktestRecord]] = {}
        self._lock = threading.Lock()

    def list(self, workspace_id: str) -> tuple[BacktestRecord, ...]:
        with self._lock:
            return tuple(reversed(tuple(self._records.get(workspace_id, {}).values())))

    def get(self, workspace_id: str, job_id: str) -> BacktestRecord | None:
        with self._lock:
            return self._records.get(workspace_id, {}).get(job_id)

    def add(self, record: BacktestRecord) -> BacktestRecord:
        with self._lock:
            bucket = self._records.setdefault(record.workspace_id, {})
            existing = bucket.get(record.job_id)
            if existing is not None:
                return existing
            bucket[record.job_id] = record
            return record
