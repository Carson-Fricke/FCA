"""Authenticated ensure and job polling routes for the market-lake API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from lake.catalog.memory import InMemoryJobRepository
from lake.catalog.models import JobRecord, JobState
from lake.catalog.ports import CatalogRepository, JobRepository
from lake.coverage import CoverageReport
from lake.coverage.application import (
    CoverageRequestError,
    EnsureJob,
    EnsureJobLauncher,
    EnsureSpec,
    ensure_coverage,
    ensure_job_id,
)
from lake.http.models import EnsureRequest, StrictModel
from lake.http.routes.catalog import (
    CatalogCoverageResponse,
    CoverageEntryResponse,
    get_catalog_max_range_days,
    get_catalog_repository,
    get_coverage_as_of,
)


class EnsureAcceptedResponse(StrictModel):
    """A bounded ensure request that continues as an asynchronous job."""

    job_id: str
    state: JobState


class JobStatusResponse(StrictModel):
    """The persisted state of one ensure job."""

    job_id: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None


def get_job_repository(request: Request) -> JobRepository:
    """Resolve the injected job repository from application state."""
    repository = getattr(request.app.state, "job_repository", None)
    if repository is None:
        repository = InMemoryJobRepository()
        request.app.state.job_repository = repository
    return cast(JobRepository, repository)


def get_job_launcher(request: Request) -> EnsureJobLauncher:
    """Resolve the injected launcher, defaulting to a provider-free no-op."""
    launcher = getattr(request.app.state, "job_launcher", None) or _noop_launcher
    return cast(EnsureJobLauncher, launcher)


def get_ensure_wait_deadline_seconds(request: Request) -> float:
    """Resolve and validate the maximum launcher wait time."""
    value = getattr(request.app.state, "ensure_wait_deadline_seconds", 20)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("ensure_wait_deadline_seconds must be a positive number")
    return float(value)


def create_ensure_router() -> APIRouter:
    """Create ensure and polling routes without binding storage or a queue."""
    router = APIRouter(prefix="/v1", tags=["ensure"])

    @router.post(
        "/catalog/ensure",
        response_model=CatalogCoverageResponse | EnsureAcceptedResponse,
        responses={202: {"model": EnsureAcceptedResponse}},
    )
    async def ensure(
        request: EnsureRequest,
        repository: Annotated[JobRepository, Depends(get_job_repository)],
        launcher: Annotated[EnsureJobLauncher, Depends(get_job_launcher)],
        max_range_days: Annotated[int, Depends(get_catalog_max_range_days)],
        as_of: Annotated[datetime, Depends(get_coverage_as_of)],
        wait_deadline_seconds: Annotated[
            float, Depends(get_ensure_wait_deadline_seconds)
        ],
        catalog_repository: Annotated[
            CatalogRepository, Depends(get_catalog_repository)
        ],
    ) -> CatalogCoverageResponse | JSONResponse:
        """Return existing coverage or launch bounded asynchronous ensure work."""
        try:
            result = await ensure_coverage(
                catalog_repository,
                repository,
                launcher,
                EnsureSpec(
                    asset_class=request.asset_class,
                    data_type=request.data_type,
                    symbol=request.symbol,
                    start_date=request.start_date,
                    end_date=request.end_date,
                ),
                max_range_days=max_range_days,
                as_of=as_of,
                wait_deadline_seconds=wait_deadline_seconds,
            )
        except CoverageRequestError as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": error.code,
                    "message": str(error),
                    **({"details": error.details} if error.details is not None else {}),
                },
            ) from error
        if isinstance(result, CoverageReport):
            return _coverage_response(result, request)
        return _accepted_response(result)

    @router.get(
        "/jobs/{job_id}",
        response_model=JobStatusResponse,
    )
    async def job_status(
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        repository: Annotated[JobRepository, Depends(get_job_repository)],
    ) -> JobStatusResponse:
        """Return the current state of one persisted ensure job."""
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "job_not_found",
                    "message": "the requested job does not exist",
                },
            )
        return JobStatusResponse(
            job_id=record.job_id,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            error_message=record.error_message,
        )

    return router


def _coverage_response(
    report: CoverageReport,
    request: EnsureRequest,
) -> CatalogCoverageResponse:
    return CatalogCoverageResponse(
        asset_class=report.asset_class,
        data_type=request.data_type,
        symbol=request.symbol.strip().upper(),
        start_date=report.start,
        end_date=report.end,
        entries=tuple(
            CoverageEntryResponse(
                session_date=entry.session_date,
                open_time=entry.session.open_time,
                close_time=entry.session.close_time,
                is_partial=entry.session.is_partial,
                status=entry.state,
                object_path=None if entry.record is None else entry.record.object_path,
                row_count=None if entry.record is None else entry.record.row_count,
                byte_size=None if entry.record is None else entry.record.byte_size,
                checksum=None if entry.record is None else entry.record.checksum,
                error_message=(
                    None if entry.record is None else entry.record.error_message
                ),
            )
            for entry in report.entries
        ),
    )


def _accepted_response(record: JobRecord) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content=EnsureAcceptedResponse(
            job_id=record.job_id,
            state=record.state,
        ).model_dump(mode="json"),
        headers={"Location": f"/v1/jobs/{record.job_id}"},
    )


def _noop_launcher(job: EnsureJob) -> None:
    del job


__all__ = [
    "EnsureAcceptedResponse",
    "EnsureJob",
    "EnsureJobLauncher",
    "JobStatusResponse",
    "create_ensure_router",
    "ensure_job_id",
    "get_ensure_wait_deadline_seconds",
    "get_job_launcher",
    "get_job_repository",
]
