"""Backtest data HTTP surface delegating launch to the harness adapter.

Listing and detail read the workspace backtest records persisted locally; the
launch route delegates to the harness activation adapter interface
(:class:`HarnessBoundary`) exactly like every other Thesis activation call.
The router resolves its services from app state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.integrations.harness_activation import (
    ActivationEnvironment,
    ActivationOutcome,
    CapabilityUnavailableError,
    HarnessActivationError,
    HarnessActivationRequest,
    HarnessBoundary,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class BacktestRecord:
    """One workspace backtest row with its detail, split, and series."""

    id: str
    workspace_id: str
    strategy_id: str
    name: str
    period_start: str
    period_end: str
    created_at: str
    initial_capital: float
    final_capital: float
    total_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trades: int
    split: Mapping[str, object] | None = None
    manifest: Mapping[str, object] = field(default_factory=dict)
    equity: tuple[Mapping[str, float | int], ...] = ()
    trade_log: tuple[Mapping[str, object], ...] = ()
    stats: Mapping[str, float] = field(default_factory=dict)

    @property
    def split_declared(self) -> bool:
        return self.split is not None

    def summary_row(self) -> dict[str, object]:
        """Return the BacktestSummaryRow projection."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "strategy_id": self.strategy_id,
            "name": self.name,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "capital": self.initial_capital,
            "total_return": self.total_return,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "trades": self.trades,
            "split_declared": self.split is not None,
            "created_at": self.created_at,
        }

    def _stats_document(self) -> dict[str, object]:
        raw = self.manifest.get("stats")
        if isinstance(raw, Mapping):
            return dict(raw)
        return dict(self._default_stats())

    def detail(self) -> dict[str, object]:
        """Return the BacktestDetail projection."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "strategy_id": self.strategy_id,
            "name": self.name,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "net_pnl": round(self.final_capital - self.initial_capital, 2),
            "equity": [dict(point) for point in self.equity],
            "trades": [dict(trade) for trade in self.trade_log],
            "stats": self._stats_document(),
            "split": dict(self.split) if self.split is not None else None,
            "manifest": dict(self.manifest),
        }

    def _default_stats(self) -> dict[str, float]:
        return {
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
        }


class BacktestStore(Protocol):
    """Read boundary over the locally persisted backtest records."""

    def list(self, workspace_id: str) -> tuple[BacktestRecord, ...]:
        """Return every record for the workspace, newest first."""
        ...

    def get(self, workspace_id: str, backtest_id: str) -> BacktestRecord | None:
        """Return one record, or ``None`` when absent."""
        ...

    def add(self, record: BacktestRecord) -> BacktestRecord:
        """Persist and return one record."""
        ...


class InMemoryBacktestStore:
    """Local in-memory backtest store."""

    def __init__(self) -> None:
        self._records: dict[str, list[BacktestRecord]] = {}

    def list(self, workspace_id: str) -> tuple[BacktestRecord, ...]:
        return tuple(self._records.get(workspace_id, ()))

    def get(self, workspace_id: str, backtest_id: str) -> BacktestRecord | None:
        for record in self._records.get(workspace_id, ()):
            if record.id == backtest_id:
                return record
        return None

    def add(self, record: BacktestRecord) -> BacktestRecord:
        self._records.setdefault(record.workspace_id, []).insert(0, record)
        return record


class LaunchBacktestRequest(BaseModel):
    """Request boundary mirroring the harness activation request contract."""

    model_config = ConfigDict(extra="forbid")

    bundle_ref: str = Field(min_length=1, max_length=256)
    config_path: str = Field(min_length=1, max_length=2_048)
    strategy_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    environment: ActivationEnvironment = "paper"


class BacktestDataService:
    """Backtest reads over local records and launch via the adapter seam."""

    def __init__(
        self,
        store: BacktestStore | None = None,
        *,
        harness: HarnessBoundary | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryBacktestStore()
        self._harness = harness
        self._clock = clock if clock is not None else _utc_now
        self._id_factory = id_factory

    @property
    def store(self) -> BacktestStore:
        return self._store

    @property
    def harness(self) -> HarnessBoundary | None:
        return self._harness

    def list_backtests(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(record.summary_row() for record in self._store.list(workspace_id))

    def get_backtest(self, workspace_id: str, backtest_id: str) -> dict[str, object]:
        record = self._store.get(workspace_id, backtest_id)
        if record is None:
            raise KeyError(backtest_id)
        return record.detail()

    def launch(self, request: LaunchBacktestRequest) -> dict[str, object]:
        """Delegate one backtest activation to the harness adapter interface."""
        if self._harness is None:
            raise CapabilityUnavailableError(
                "backtest", "no harness adapter configured"
            )
        activation = HarnessActivationRequest(
            bundle_ref=request.bundle_ref,
            config_path=request.config_path,
            action="backtest",
            environment=request.environment,
        )
        outcome = self._harness.run_backtest(activation)
        return _outcome_document(outcome)


def _outcome_document(outcome: ActivationOutcome) -> dict[str, object]:
    return {
        "action": outcome.action,
        "bundle_ref": outcome.bundle_ref,
        "environment": outcome.environment,
        "exit_code": outcome.exit_code,
        "job_id": outcome.job_id,
        "state": outcome.state,
        "request_payload": dict(outcome.request_payload),
    }


def get_backtest_service(request: Request) -> BacktestDataService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "backtest_service", None)
    if service is None:
        service = BacktestDataService()
        request.app.state.backtest_service = service
    if not isinstance(service, BacktestDataService):
        raise RuntimeError("app.state.backtest_service must be a BacktestDataService")
    return service


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(
            status_code=404,
            detail={
                "code": "backtest_not_found",
                "message": f"backtest {error.args[0]!r} does not exist",
            },
        )
    if isinstance(error, (CapabilityUnavailableError, HarnessActivationError)):
        return HTTPException(
            status_code=503,
            detail={"code": "harness_unavailable", "message": str(error)},
        )
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(
        status_code=500, detail={"code": "backtest_error", "message": "request failed"}
    )


def create_backtest_router() -> APIRouter:
    """Create the backtest router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["backtests"])

    @router.get("/backtests")
    async def list_backtests(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[BacktestDataService, Depends(get_backtest_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_backtests(workspace_id))}

    @router.get("/backtests/{backtest_id}")
    async def get_backtest(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        backtest_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[BacktestDataService, Depends(get_backtest_service)],
    ) -> dict[str, object]:
        try:
            return service.get_backtest(workspace_id, backtest_id)
        except Exception as error:
            raise _service_error(error) from error

    @router.post("/backtests/launch")
    async def launch_backtest(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: LaunchBacktestRequest,
        service: Annotated[BacktestDataService, Depends(get_backtest_service)],
    ) -> dict[str, object]:
        del workspace_id
        try:
            return service.launch(request)
        except Exception as error:
            raise _service_error(error) from error

    return router


__all__ = [
    "BacktestDataService",
    "BacktestRecord",
    "BacktestStore",
    "InMemoryBacktestStore",
    "LaunchBacktestRequest",
    "create_backtest_router",
    "get_backtest_service",
]
