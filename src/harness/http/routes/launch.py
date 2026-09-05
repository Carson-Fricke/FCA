"""Authenticated backtest launch and job polling routes."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from harness.http.launcher import (
    BacktestJobLauncher,
    BacktestLaunchJob,
    InProcessBacktestLauncher,
    parse_backtest_configuration_json,
)
from harness.http.models import BacktestJobStatusResponse, LaunchAcceptedResponse
from harness.http.routes.runs import get_results_store
from lake.catalog.memory import InMemoryJobRepository
from lake.catalog.models import JobRecord, JobState
from lake.catalog.ports import JobRepository


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_job_repository(request: Request) -> JobRepository:
    """Resolve the injected job repository from application state."""
    repository = getattr(request.app.state, "job_repository", None)
    if repository is None:
        # This fallback keeps a directly mounted router deterministic while
        # the application factory supplies the normal dependency.
        repository = InMemoryJobRepository()
        request.app.state.job_repository = repository
    return cast(JobRepository, repository)


def get_job_launcher(request: Request) -> BacktestJobLauncher:
    """Resolve the injected launcher, defaulting to in-process execution."""
    launcher = getattr(request.app.state, "job_launcher", None)
    if launcher is None:
        launcher = InProcessBacktestLauncher(
            get_results_store(request),
            get_job_repository(request),
        )
        request.app.state.job_launcher = launcher
    return cast(BacktestJobLauncher, launcher)


def get_clock(request: Request) -> Callable[[], datetime]:
    """Resolve the injectable clock used for job record timestamps."""
    clock = getattr(request.app.state, "clock", None) or _utc_now
    return cast(Callable[[], datetime], clock)


def _error_message(error: BaseException) -> str:
    detail = str(error).strip()
    return type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"


def _mark_failed(
    jobs: JobRepository,
    job_id: str,
    clock: Callable[[], datetime],
    error: Exception,
) -> JobRecord:
    """Persist one failed launch while preserving the original creation time."""
    existing = jobs.get(job_id)
    return jobs.upsert(
        JobRecord(
            job_id=job_id,
            state=JobState.FAILED,
            created_at=existing.created_at if existing is not None else clock(),
            updated_at=clock(),
            error_message=_error_message(error),
        )
    )


def _accepted_response(record: JobRecord) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content=LaunchAcceptedResponse(
            job_id=record.job_id,
            state=record.state,
        ).model_dump(mode="json"),
        headers={"Location": f"/v1/backtests/jobs/{record.job_id}"},
    )


def create_launch_router() -> APIRouter:
    """Create the launch and polling routes without binding them to a queue."""
    router = APIRouter(prefix="/v1/backtests", tags=["backtests"])

    @router.post(
        "/jobs",
        response_model=LaunchAcceptedResponse,
        responses={202: {"model": LaunchAcceptedResponse}},
    )
    async def launch_job(
        request: Request,
        jobs: Annotated[JobRepository, Depends(get_job_repository)],
        launcher: Annotated[BacktestJobLauncher, Depends(get_job_launcher)],
        clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    ) -> JSONResponse:
        """Validate the submitted configuration and launch one backtest job.

        The body is one backtest configuration document. It is decoded with
        exact-decimal JSON semantics, so typed decimals must arrive as JSON
        numbers and parameter-tree values as strings, exactly mirroring the
        harness YAML configuration conventions.
        """
        body = await request.body()
        try:
            configuration = parse_backtest_configuration_json(body)
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "validation_error",
                    "message": (
                        "the submitted document is not a valid backtest configuration"
                    ),
                    "details": str(error),
                },
            ) from error
        job_id = configuration.run_id
        now = clock()
        record = jobs.upsert(
            JobRecord(
                job_id=job_id,
                state=JobState.QUEUED,
                created_at=now,
                updated_at=now,
            )
        )
        try:
            outcome = launcher(
                BacktestLaunchJob(job_id=job_id, configuration=configuration)
            )
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as error:
            return _accepted_response(_mark_failed(jobs, job_id, clock, error))
        return _accepted_response(jobs.get(job_id) or record)

    @router.get(
        "/jobs/{job_id}",
        response_model=BacktestJobStatusResponse,
    )
    async def job_status(
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        jobs: Annotated[JobRepository, Depends(get_job_repository)],
    ) -> BacktestJobStatusResponse:
        """Return the current state of one persisted backtest launch job."""
        record = jobs.get(job_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "job_not_found",
                    "message": "the requested job does not exist",
                },
            )
        return BacktestJobStatusResponse(
            job_id=record.job_id,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            error_message=record.error_message,
        )

    return router


__all__ = [
    "create_launch_router",
    "get_clock",
    "get_job_launcher",
    "get_job_repository",
]
