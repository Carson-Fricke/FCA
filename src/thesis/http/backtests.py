"""Backtest data HTTP surface delegating launch to the harness adapter.

Listing discovers retained execution references, while report detail reads
published evidence keyed by the harness ``job_id``. The launch route delegates to the harness
activation adapter interface (:class:`HarnessBoundary`) exactly like every
other Thesis activation call.  The router resolves its services from app state.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.artifacts.importer import (
    ArtifactImporter,
    ArtifactImportError,
    ArtifactImportReceipt,
    HarnessManifest,
)
from thesis.backtests.catalog import BacktestCatalog
from thesis.backtests.records import (
    BacktestRecord,
    BacktestStore,
    InMemoryBacktestStore,
)
from thesis.integrations.harness_activation import (
    ActivationEnvironment,
    ActivationOutcome,
    CapabilityUnavailableError,
    HarnessActivationError,
    HarnessActivationProtocolError,
    HarnessActivationRequest,
    HarnessBoundary,
)
from thesis.integrations.mcp import HarnessMcpError
from thesis.persistence.submissions import SubmissionStore, retain_submission


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LaunchBacktestRequest(BaseModel):
    """Request boundary mirroring the harness activation request contract."""

    model_config = ConfigDict(extra="forbid")

    bundle_ref: str = Field(min_length=1, max_length=256)
    config: dict[str, object] = Field(min_length=1)
    strategy_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    environment: ActivationEnvironment = "paper"


class BacktestDataService:
    """Backtest reads over local records and launch via the adapter seam.

    A launch persists a :class:`BacktestRecord` derived from the harness
    outcome under the submitted ``job_id``, so the job immediately appears in
    the list and detail reads.  When the outcome carries a harness
    reproducibility manifest and an
    :class:`~thesis.artifacts.importer.ArtifactImporter` is injected, the
    reproducibility-critical artifacts are imported and referenced by content
    hash; without a manifest the import step is skipped cleanly.
    """

    def __init__(
        self,
        store: BacktestStore | None = None,
        *,
        harness: HarnessBoundary | None = None,
        submissions: SubmissionStore | None = None,
        importer: ArtifactImporter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.submissions = submissions
        self._store = store if store is not None else InMemoryBacktestStore()
        self._harness = harness
        self._importer = importer
        self._clock = clock if clock is not None else _utc_now

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

    def get_backtest(self, workspace_id: str, job_id: str) -> dict[str, object]:
        record = self._store.get(workspace_id, job_id)
        if record is None:
            raise KeyError(job_id)
        return record.detail()

    def launch(
        self, workspace_id: str, request: LaunchBacktestRequest
    ) -> dict[str, object]:
        """Delegate one backtest activation and return its launch card.

        The job id is allocated before dispatch and retained on every failure
        path so an unknown outcome stays inspectable by its original id.  Only
        terminal outcomes are persisted; a non-terminal launch is projected by
        the materialize endpoint once the harness reports a terminal state.
        """
        if self._harness is None:
            raise CapabilityUnavailableError(
                "backtest", "no harness adapter configured"
            )
        job_id = f"job-{uuid4().hex}"
        config = {**request.config, "job_id": job_id}
        activation = HarnessActivationRequest(
            bundle_ref=request.bundle_ref,
            config_path="",
            config=config,
            action="backtest",
            environment=request.environment,
            job_id=job_id,
        )
        retain_submission(
            self.submissions,
            workspace_id,
            job_id,
            "run_backtest",
            {**request.model_dump(mode="json"), "config": config},
        )
        try:
            outcome = self._harness.run_backtest(activation)
        except (HarnessActivationError, HarnessMcpError) as error:
            error.job_id = job_id
            raise
        returned = _text(outcome.job_id)
        if returned is None or returned != job_id:
            error = HarnessActivationProtocolError(
                "harness launch outcome requires the submitted job_id "
                f"{job_id!r}, got {returned!r}"
            )
            error.job_id = job_id
            raise error
        record = self._record_for_outcome(workspace_id, request, outcome, job_id)
        record = self._import_artifacts(record, outcome)
        if record.status in _TERMINAL_JOB_STATES:
            record = self._store.add(record)
        document = _outcome_document(outcome)
        document["backtest"] = record.detail()
        return document

    def _record_for_outcome(
        self,
        workspace_id: str,
        request: LaunchBacktestRequest,
        outcome: ActivationOutcome,
        job_id: str,
    ) -> BacktestRecord:
        """Project one launch outcome onto a record without deriving any statistic.

        Statistics are read from the canonical ``equity`` and ``trades`` blocks of
        the outcome's ``metrics`` document, as the harness published them; a value
        those blocks do not publish stays ``None``.
        """
        raw = outcome.raw
        metrics = _mapping(raw.get("metrics"))
        equity = _mapping(metrics.get("equity"))
        trades = _mapping(metrics.get("trades"))
        manifest = _mapping(raw.get("manifest"))
        strategy_id = (
            request.strategy_id
            or _text(metrics.get("strategy_id"))
            or f"strategy-{job_id}"
        )
        name = request.name or _text(metrics.get("name")) or f"Backtest {job_id}"
        period_start = _first_text(
            metrics, "period_start", "periodStart", "data_start", "dataStart"
        )
        period_end = _first_text(
            metrics, "period_end", "periodEnd", "data_end", "dataEnd"
        )
        launch_document: dict[str, object] = {
            "bundle_ref": request.bundle_ref,
            "config": request.config,
            "environment": request.environment,
        }
        manifest_document = dict(manifest)
        manifest_document["launch"] = launch_document
        launch_document["job_id"] = job_id
        if outcome.state is not None:
            launch_document["state"] = outcome.state
        if outcome.exit_code is not None:
            launch_document["exit_code"] = outcome.exit_code
        if metrics:
            manifest_document["metrics"] = metrics
        return BacktestRecord(
            job_id=job_id,
            workspace_id=workspace_id,
            strategy_id=strategy_id,
            name=name,
            period_start=period_start,
            period_end=period_end,
            created_at=_iso_z(self._clock()),
            # Canonical metrics publish no capital; the artifact import reads it
            # from backtest.json, which a launch receipt does not carry.
            initial_capital=None,
            final_capital=None,
            total_return=_optional_number(equity.get("total_return")),
            sharpe_per_sample=_optional_number(equity.get("sharpe_per_sample")),
            max_drawdown=_optional_number(equity.get("max_drawdown")),
            win_rate=_optional_number(trades.get("hit_rate")),
            trades=_optional_count(trades.get("trade_count")),
            split=_optional_mapping(metrics.get("split")),
            manifest=manifest_document,
            status=_launch_status(outcome),
            bundle_ref=request.bundle_ref,
            config_path="config.json",
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
            experiment_id=manifest.experiment_id or record.job_id,
            workflow_execution_id=manifest.workflow_execution_id or record.job_id,
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


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _optional_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _launch_status(outcome: ActivationOutcome) -> str:
    state = _text(outcome.state)
    if state is not None:
        return state
    if outcome.exit_code is None:
        return "queued"
    return "succeeded" if outcome.exit_code == 0 else "failed"


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
    from thesis.http.errors import require_service

    return require_service(request, "backtest_service", BacktestDataService)


def get_backtest_catalog(request: Request) -> BacktestCatalog:
    from thesis.http.errors import require_service

    return require_service(request, "backtest_catalog", BacktestCatalog)


def _service_error(error: Exception) -> HTTPException:
    from thesis.http.errors import service_error

    return service_error(error, resource="backtest")


def create_backtest_router() -> APIRouter:
    """Create the backtest router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["backtests"])

    @router.get("/backtests")
    def list_backtests(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        catalog: Annotated[BacktestCatalog, Depends(get_backtest_catalog)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        strategy_id: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, object]:
        return catalog.page(
            workspace_id, offset=offset, limit=limit, strategy_id=strategy_id
        )

    @router.get("/backtests/jobs/{job_id}/inspection")
    def inspect_backtest_job(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        catalog: Annotated[BacktestCatalog, Depends(get_backtest_catalog)],
    ) -> dict[str, object]:
        """Inspect retained identity, actual execution and frozen inputs without import."""
        try:
            return catalog.get(workspace_id, job_id)
        except (KeyError, ValueError) as error:
            raise _service_error(error) from error

    @router.get("/backtests/jobs/{job_id}")
    def materialize_backtest_job(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: Request,
        chat_id: Annotated[str | None, Query(max_length=128)] = None,
        execution_id: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, object]:
        """Materialize one harness job into workspace card data.

        The UI calls this once the harness MCP tool returns a job id.  The
        harness boundary (local or remote) is owned here, so the same call
        yields the report for local and Azure execution.
        """
        coordinator = getattr(request.app.state, "backtest_run_coordinator", None)
        if coordinator is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "backtest_coordinator_unavailable",
                    "message": "the backtest run coordinator is not composed",
                },
            )
        try:
            return coordinator.materialize(
                workspace_id, job_id, chat_id=chat_id, execution_id=execution_id
            )
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/backtests/{job_id}")
    def get_backtest(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[BacktestDataService, Depends(get_backtest_service)],
        request: Request,
    ) -> dict[str, object]:
        try:
            return service.get_backtest(workspace_id, job_id)
        except KeyError:
            # Report caching is optional. A terminal job can be opened directly
            # from its retained reference without first visiting its chat card.
            catalog = get_backtest_catalog(request)
            try:
                observation = catalog.get(workspace_id, job_id)
            except KeyError as error:
                raise _service_error(error) from error
            coordinator = getattr(request.app.state, "backtest_run_coordinator", None)
            if observation["state"] != "succeeded" or coordinator is None:
                raise _service_error(KeyError(job_id)) from None
            card = coordinator.materialize(workspace_id, job_id, state="succeeded")
            backtest = card.get("backtest")
            if not isinstance(backtest, dict):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "backtest_report_unavailable",
                        "message": card.get("error")
                        or "Published report is temporarily unavailable; the execution remains visible.",
                    },
                ) from None
            return backtest
        except Exception as error:
            raise _service_error(error) from error

    @router.post("/backtests/launch", status_code=202)
    def launch_backtest(
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
    "LaunchBacktestRequest",
    "create_backtest_router",
    "get_backtest_service",
]
