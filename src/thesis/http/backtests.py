"""Backtest data HTTP surface delegating launch to the harness adapter.

Listing and detail read the workspace backtest records persisted locally; the
launch route delegates to the harness activation adapter interface
(:class:`HarnessBoundary`) exactly like every other Thesis activation call.
The router resolves its services from app state.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.artifacts.importer import (
    ArtifactImporter,
    ArtifactImportError,
    ArtifactImportReceipt,
    HarnessManifest,
)
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
    status: str = "completed"
    bundle_ref: str | None = None
    config_path: str | None = None

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
            "status": self.status,
            "bundle_ref": self.bundle_ref,
            "config_path": self.config_path,
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
    """Backtest reads over local records and launch via the adapter seam.

    A launch persists a :class:`BacktestRecord` derived from the harness
    outcome, so the run immediately appears in the list and detail reads.  When
    the outcome carries a harness reproducibility manifest and an
    :class:`~thesis.artifacts.importer.ArtifactImporter` is injected, the
    reproducibility-critical artifacts are imported and referenced by content
    hash; without a manifest the import step is skipped cleanly.
    """

    def __init__(
        self,
        store: BacktestStore | None = None,
        *,
        harness: HarnessBoundary | None = None,
        importer: ArtifactImporter | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryBacktestStore()
        self._harness = harness
        self._importer = importer
        self._clock = clock if clock is not None else _utc_now
        self._id_factory = id_factory

    @property
    def store(self) -> BacktestStore:
        return self._store

    @property
    def harness(self) -> HarnessBoundary | None:
        return self._harness

    @property
    def importer(self) -> ArtifactImporter | None:
        return self._importer

    def list_backtests(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(record.summary_row() for record in self._store.list(workspace_id))

    def get_backtest(self, workspace_id: str, backtest_id: str) -> dict[str, object]:
        record = self._store.get(workspace_id, backtest_id)
        if record is None:
            raise KeyError(backtest_id)
        return record.detail()

    def launch(
        self, workspace_id: str, request: LaunchBacktestRequest
    ) -> dict[str, object]:
        """Delegate one backtest activation, then persist the outcome record."""
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
        record = self._record_for_outcome(workspace_id, request, outcome)
        record = self._import_artifacts(record, outcome)
        self._store.add(record)
        document = _outcome_document(outcome)
        document["backtest"] = record.detail()
        document["backtest_id"] = record.id
        return document

    def _new_backtest_id(self) -> str:
        if self._id_factory is not None:
            return self._id_factory("bt")
        return f"bt-{uuid4().hex}"

    def _record_for_outcome(
        self,
        workspace_id: str,
        request: LaunchBacktestRequest,
        outcome: ActivationOutcome,
    ) -> BacktestRecord:
        backtest_id = self._new_backtest_id()
        raw = outcome.raw
        metrics = _mapping(raw.get("metrics"))
        manifest = _mapping(raw.get("manifest"))
        strategy_id = (
            request.strategy_id
            or _text(metrics.get("strategy_id"))
            or (f"strategy-{backtest_id}")
        )
        name = request.name or _text(metrics.get("name")) or f"Backtest {backtest_id}"
        period_start = _first_text(
            metrics, "period_start", "periodStart", "data_start", "dataStart"
        )
        period_end = _first_text(
            metrics, "period_end", "periodEnd", "data_end", "dataEnd"
        )
        launch_document: dict[str, object] = {
            "bundle_ref": request.bundle_ref,
            "config_path": request.config_path,
            "environment": request.environment,
        }
        manifest_document = dict(manifest)
        manifest_document["launch"] = launch_document
        if outcome.job_id is not None:
            launch_document["job_id"] = outcome.job_id
        if outcome.state is not None:
            launch_document["state"] = outcome.state
        if outcome.exit_code is not None:
            launch_document["exit_code"] = outcome.exit_code
        if metrics:
            manifest_document["metrics"] = metrics
        return BacktestRecord(
            id=backtest_id,
            workspace_id=workspace_id,
            strategy_id=strategy_id,
            name=name,
            period_start=period_start,
            period_end=period_end,
            created_at=_iso_z(self._clock()),
            initial_capital=_number(metrics.get("initial_capital"), 0.0),
            final_capital=_number(metrics.get("final_capital"), 0.0),
            total_return=_number(
                _first(metrics, "total_return", "totalReturn", "return_pct"), 0.0
            ),
            sharpe=_number(metrics.get("sharpe"), 0.0),
            max_drawdown=_number(_first(metrics, "max_drawdown", "maxDrawdown"), 0.0),
            win_rate=_number(_first(metrics, "win_rate", "winRate"), 0.0),
            trades=_count(metrics.get("trades"), 0),
            split=_optional_mapping(metrics.get("split")),
            manifest=manifest_document,
            status=_launch_status(outcome),
            bundle_ref=request.bundle_ref,
            config_path=request.config_path,
        )

    def _import_artifacts(
        self, record: BacktestRecord, outcome: ActivationOutcome
    ) -> BacktestRecord:
        raw = outcome.raw
        manifest_value = raw.get("manifest")
        if self._importer is None or manifest_value is None:
            return record
        manifest = _harness_manifest(manifest_value)
        receipt = self._importer.import_manifest(
            manifest,
            strategy_version=manifest.strategy_version or "0.0.0",
            experiment_id=manifest.experiment_id or record.id,
            workflow_execution_id=manifest.workflow_execution_id or record.id,
            source_root=_source_root(raw),
        )
        manifest_document = dict(record.manifest)
        manifest_document["import"] = _receipt_document(receipt)
        return replace(record, manifest=manifest_document)


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


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first(source: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        if name in source:
            return source[name]
    return None


def _first_text(source: Mapping[str, object], *names: str) -> str:
    return _text(_first(source, *names)) or ""


def _number(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _count(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _launch_status(outcome: ActivationOutcome) -> str:
    state = _text(outcome.state)
    if state is not None:
        return state
    if outcome.exit_code is None:
        return "launched"
    return "completed" if outcome.exit_code == 0 else "failed"


def _harness_manifest(value: object) -> HarnessManifest:
    if isinstance(value, HarnessManifest):
        return value
    if isinstance(value, Mapping):
        return HarnessManifest.from_mapping(value)
    raise ArtifactImportError("harness manifest must be an object")


def _source_root(raw: Mapping[str, object]) -> str | None:
    value = _first(raw, "result_root", "source_root", "workspace")
    text = _text(value)
    if text is not None:
        return text
    if isinstance(value, pathlib.Path):
        return str(value)
    return None


def _receipt_document(receipt: ArtifactImportReceipt) -> dict[str, object]:
    return {
        "artifacts": [reference.as_dict() for reference in receipt.artifacts],
        "experimentId": receipt.experiment_id,
        "gitCommit": receipt.git_commit,
        "harnessManifest": {
            "id": receipt.harness_manifest_id,
            "sha256": receipt.harness_manifest_sha256,
        },
        "provenanceUri": receipt.provenance_uri,
        "strategyVersion": receipt.strategy_version,
        "workflowExecutionId": receipt.workflow_execution_id,
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
    if isinstance(error, ArtifactImportError):
        return HTTPException(
            status_code=500,
            detail={"code": "artifact_import_failed", "message": str(error)},
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
        try:
            return service.launch(workspace_id, request)
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
