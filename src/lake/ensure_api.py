"""Authenticated ensure and job polling routes for the market-lake API."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Thread
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from lake.catalog import (
    CatalogRepository,
    InMemoryJobRepository,
    JobRecord,
    JobRepository,
    JobState,
)
from lake.catalog_api import (
    CatalogCoverageResponse,
    CoverageEntryResponse,
    get_catalog_max_range_days,
    get_catalog_repository,
    get_coverage_as_of,
)
from lake.coverage import CoverageReport, SessionCoverage, calculate_coverage
from lake.models import EnsureRequest, StrictModel
from lake.partition_paths import AssetClass


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


@dataclass(frozen=True, slots=True)
class EnsureJob:
    """The provider-independent work description passed to a job launcher."""

    job_id: str
    request: EnsureRequest
    gaps: tuple[SessionCoverage, ...]


EnsureJobLauncher = Callable[[EnsureJob], None | Awaitable[None]]


def ensure_job_id(request: EnsureRequest) -> str:
    """Return the stable identifier for a logically equivalent ensure request."""
    payload = request.model_dump(mode="json")
    payload["symbol"] = request.symbol.strip().upper()
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"ensure-{hashlib.sha256(encoded).hexdigest()}"


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
        requested_days = (request.end_date - request.start_date).days + 1
        if requested_days > max_range_days:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "range_too_large",
                    "message": "requested date range exceeds the ensure limit",
                    "details": {
                        "max_range_days": max_range_days,
                        "requested_range_days": requested_days,
                    },
                },
            )
        if request.asset_class not in {AssetClass.EQUITY, AssetClass.CRYPTO}:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "unsupported_asset_class",
                    "message": "ensure planning supports equity and crypto only",
                },
            )

        report = calculate_coverage(
            catalog_repository,
            asset_class=request.asset_class,
            symbols=request.symbol,
            data_types=request.data_type,
            start=request.start_date,
            end=request.end_date,
            as_of=as_of,
        )
        if report.is_satisfied:
            return _coverage_response(report, request)

        job_id = ensure_job_id(request)
        existing = repository.get(job_id)
        if existing is not None:
            return _accepted_response(existing)

        record = repository.upsert(
            JobRecord(
                job_id=job_id,
                state=JobState.QUEUED,
                created_at=as_of,
                updated_at=as_of,
            )
        )
        job = EnsureJob(job_id=job_id, request=request, gaps=report.gap_plan().gaps)
        completed = False
        try:
            completed = await _invoke_launcher(
                launcher,
                job,
                timeout=wait_deadline_seconds,
            )
        except Exception as error:
            record = repository.upsert(
                JobRecord(
                    job_id=record.job_id,
                    state=JobState.FAILED,
                    created_at=record.created_at,
                    updated_at=as_of,
                    error_message=str(error),
                )
            )
        if completed:
            refreshed = calculate_coverage(
                catalog_repository,
                asset_class=request.asset_class,
                symbols=request.symbol,
                data_types=request.data_type,
                start=request.start_date,
                end=request.end_date,
                as_of=as_of,
            )
            if refreshed.is_satisfied:
                return _coverage_response(refreshed, request)

        current = repository.get(job_id) or record
        return JSONResponse(
            status_code=202,
            content=EnsureAcceptedResponse(
                job_id=current.job_id,
                state=current.state,
            ).model_dump(mode="json"),
            headers={"Location": f"/v1/jobs/{current.job_id}"},
        )

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


async def _invoke_launcher(
    launcher: EnsureJobLauncher,
    job: EnsureJob,
    *,
    timeout: float,
) -> bool:
    completed = Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            result = launcher(job)
            if inspect.isawaitable(result):
                asyncio.run(cast(Coroutine[Any, Any, None], result))
        except Exception as error:
            errors.append(error)
        finally:
            completed.set()

    Thread(target=run, daemon=True).start()
    if not await asyncio.to_thread(completed.wait, timeout):
        return False
    if errors:
        raise errors[0]
    return True


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
