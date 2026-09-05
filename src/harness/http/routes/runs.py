"""Authenticated backtest run query routes for the quant-harness API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from harness.http.models import (
    ArtifactListResponse,
    ArtifactSummary,
    RunListResponse,
    RunResponse,
)
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
    RunCatalogRecord,
    RunStatus,
    run_prefix,
)
from lake.storage.ports import SasIssuer

_RUN_ID = Annotated[str, Path(min_length=1, max_length=128)]


def get_results_store(request: Request) -> BacktestResultsStore:
    """Resolve the results store from application state for testable wiring."""
    store = getattr(request.app.state, "results_store", None)
    if store is None:
        # This fallback keeps a directly mounted router deterministic while
        # the application factory supplies the normal dependency.
        store = BacktestResultsStore(
            InMemoryRunBlobStore(),
            InMemoryRunCatalogRepository(),
        )
        request.app.state.results_store = store
    return cast(BacktestResultsStore, store)


def get_sas_issuer(request: Request) -> SasIssuer | None:
    """Resolve the optional artifact SAS issuer from application state."""
    return cast(SasIssuer | None, getattr(request.app.state, "sas_issuer", None))


def _run_or_404(store: BacktestResultsStore, run_id: str) -> RunCatalogRecord:
    try:
        record = store.get(run_id)
    except ValueError as error:
        raise _run_not_found() from error
    if record is None:
        raise _run_not_found()
    return record


def _run_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "run_not_found",
            "message": "the requested backtest run does not exist",
        },
    )


def _run_response(record: RunCatalogRecord) -> RunResponse:
    return RunResponse(
        run_id=record.run_id,
        status=record.status,
        strategy_id=record.strategy_id,
        data_start=record.data_start,
        data_end=record.data_end,
        run_digest=record.run_digest,
        configuration_digest=record.configuration_digest,
        artifact_count=record.artifact_count,
        total_bytes=record.total_bytes,
        metrics=dict(record.metrics),
    )


def _artifact_summaries(
    run_id: str,
    artifacts: Mapping[str, bytes],
    sas_issuer: SasIssuer | None,
) -> tuple[ArtifactSummary, ...]:
    prefix = run_prefix(run_id)
    return tuple(
        ArtifactSummary(
            name=name,
            size=len(payload),
            object_path=f"{prefix}/{name}",
            sas_url=None
            if sas_issuer is None
            else sas_issuer.issue(f"{prefix}/{name}"),
        )
        for name, payload in sorted(artifacts.items())
    )


def create_runs_router() -> APIRouter:
    """Create the run query routes without binding them to a store."""
    router = APIRouter(prefix="/v1/backtests", tags=["backtests"])

    @router.get("/runs", response_model=RunListResponse)
    async def list_runs(
        store: Annotated[BacktestResultsStore, Depends(get_results_store)],
        status: Annotated[RunStatus | None, Query()] = None,
        strategy_id: Annotated[str | None, Query(max_length=128)] = None,
    ) -> RunListResponse:
        """List stored backtest runs sorted by run ID with optional filters."""
        records = store.list(status=status, strategy_id=strategy_id)
        return RunListResponse(runs=tuple(_run_response(record) for record in records))

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(
        run_id: _RUN_ID,
        store: Annotated[BacktestResultsStore, Depends(get_results_store)],
    ) -> RunResponse:
        """Return one stored run's catalog row."""
        return _run_response(_run_or_404(store, run_id))

    @router.get("/runs/{run_id}/artifacts", response_model=ArtifactListResponse)
    async def list_artifacts(
        run_id: _RUN_ID,
        store: Annotated[BacktestResultsStore, Depends(get_results_store)],
        sas_issuer: Annotated[SasIssuer | None, Depends(get_sas_issuer)],
    ) -> ArtifactListResponse:
        """List one run's stored artifacts with optional SAS read access."""
        record = _run_or_404(store, run_id)
        stored = store.fetch(record.run_id)
        return ArtifactListResponse(
            run_id=record.run_id,
            artifacts=_artifact_summaries(record.run_id, stored.artifacts, sas_issuer),
        )

    @router.get("/runs/{run_id}/artifacts/{artifact_name:path}")
    async def download_artifact(
        run_id: _RUN_ID,
        artifact_name: Annotated[str, Path(min_length=1, max_length=512)],
        store: Annotated[BacktestResultsStore, Depends(get_results_store)],
    ) -> Response:
        """Return one stored artifact's exact payload bytes."""
        record = _run_or_404(store, run_id)
        stored = store.fetch(record.run_id)
        try:
            payload = stored.artifacts[artifact_name]
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "artifact_not_found",
                    "message": "the requested artifact is not part of the run",
                },
            ) from error
        return Response(content=payload, media_type="application/octet-stream")

    @router.delete("/runs/{run_id}", status_code=204)
    async def delete_run(
        run_id: _RUN_ID,
        store: Annotated[BacktestResultsStore, Depends(get_results_store)],
    ) -> Response:
        """Remove one run's complete artifact prefix and catalog row."""
        try:
            removed = store.delete(run_id)
        except ValueError as error:
            raise _run_not_found() from error
        if not removed:
            raise _run_not_found()
        return Response(status_code=204)

    return router


__all__ = [
    "create_runs_router",
    "get_results_store",
    "get_sas_issuer",
]
