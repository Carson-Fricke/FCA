"""Authenticated ensure and job polling routes for the market-lake API."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Coroutine
from datetime import datetime
from threading import Event, Thread
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from lake.catalog.front_sets import DailyFrontSetRepository
from lake.catalog.memory import InMemoryJobRepository
from lake.catalog.models import JobRecord, JobState
from lake.catalog.ports import CatalogRepository, ContractRepository, JobRepository
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageReport, SessionCoverage
from lake.coverage.application import (
    CoverageRequestError,
    EnsureJob,
    EnsureJobLauncher,
    EnsureSpec,
    ensure_coverage,
    ensure_job_id,
)
from lake.http.models import DateRangeRequest, EnsureRequest, StrictModel, Symbol
from lake.http.routes.catalog import (
    CatalogCoverageResponse,
    CoverageEntryResponse,
    OptionCatalogCoverageResponse,
    get_catalog_max_range_days,
    get_catalog_repository,
    get_coverage_as_of,
    get_option_contract_repository,
    get_option_front_set_repository,
    option_coverage,
    option_coverage_response,
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


class OptionEnsureRequest(DateRangeRequest):
    """Strict option ensure input for an underlying or selected OSI contracts."""

    asset_class: Literal[AssetClass.OPTION]
    data_type: DataType
    symbol: Symbol | None = None
    underlying: Symbol | None = None
    contract_symbols: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    contracts: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def _validate_selection(self) -> OptionEnsureRequest:
        if self.contract_symbols is not None and self.contracts is not None:
            raise ValueError("use only one of contract_symbols or contracts")
        if (
            self.symbol is None
            and self.underlying is None
            and self.contract_symbols is None
            and self.contracts is None
        ):
            raise ValueError("an option underlying or selected contract is required")
        return self


EnsureRequestBody = OptionEnsureRequest | EnsureRequest


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
        response_model=(
            CatalogCoverageResponse
            | OptionCatalogCoverageResponse
            | EnsureAcceptedResponse
        ),
        responses={202: {"model": EnsureAcceptedResponse}},
    )
    async def ensure(
        request: EnsureRequestBody,
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
        contract_repository: Annotated[
            ContractRepository | None, Depends(get_option_contract_repository)
        ],
        front_set_repository: Annotated[
            DailyFrontSetRepository | None, Depends(get_option_front_set_repository)
        ],
    ) -> CatalogCoverageResponse | OptionCatalogCoverageResponse | JSONResponse:
        """Return existing coverage or launch bounded asynchronous ensure work."""
        try:
            if request.asset_class is AssetClass.OPTION:
                return await _ensure_option(
                    cast(OptionEnsureRequest, request),
                    catalog_repository=catalog_repository,
                    job_repository=repository,
                    launcher=launcher,
                    contract_repository=contract_repository,
                    front_set_repository=front_set_repository,
                    max_range_days=max_range_days,
                    as_of=as_of,
                    wait_deadline_seconds=wait_deadline_seconds,
                )
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


async def _ensure_option(
    request: OptionEnsureRequest,
    *,
    catalog_repository: CatalogRepository,
    job_repository: JobRepository,
    launcher: EnsureJobLauncher,
    contract_repository: ContractRepository | None,
    front_set_repository: DailyFrontSetRepository | None,
    max_range_days: int,
    as_of: datetime,
    wait_deadline_seconds: float,
) -> OptionCatalogCoverageResponse | JSONResponse:
    """Run option ensure using the same persisted job and launcher contract."""
    report, selection = option_coverage(
        catalog_repository,
        request,
        contract_repository=contract_repository,
        front_set_repository=front_set_repository,
        max_range_days=max_range_days,
        as_of=as_of,
    )
    if all(entry.is_terminal for entry in report.entries):
        return option_coverage_response(report, request.data_type, selection)

    job_id = _option_job_id(request, selection.symbols)
    existing = job_repository.get(job_id)
    if existing is not None and existing.state in {JobState.QUEUED, JobState.RUNNING}:
        return _accepted_response(existing)
    created_at = existing.created_at if existing is not None else as_of
    record = job_repository.upsert(
        JobRecord(
            job_id=job_id,
            state=JobState.RUNNING,
            created_at=created_at,
            updated_at=as_of,
        )
    )

    def finalize(error: Exception | None) -> None:
        job_repository.upsert(
            JobRecord(
                job_id=job_id,
                state=JobState.FAILED if error is not None else JobState.COMPLETE,
                created_at=created_at,
                updated_at=as_of,
                error_message=None if error is None else str(error),
            )
        )

    try:
        completed = await _invoke_option_launcher(
            launcher,
            request,
            job_id=job_id,
            gaps=report.gap_plan().gaps,
            symbols=tuple(dict.fromkeys(gap.symbol for gap in report.gap_plan().gaps)),
            timeout=wait_deadline_seconds,
            on_complete=finalize,
        )
    except Exception as error:
        record = job_repository.upsert(
            JobRecord(
                job_id=job_id,
                state=JobState.FAILED,
                created_at=created_at,
                updated_at=as_of,
                error_message=str(error),
            )
        )
        return _accepted_response(record)
    if completed:
        refreshed, refreshed_selection = option_coverage(
            catalog_repository,
            request,
            contract_repository=contract_repository,
            front_set_repository=front_set_repository,
            max_range_days=max_range_days,
            as_of=as_of,
        )
        if all(entry.is_terminal for entry in refreshed.entries):
            return option_coverage_response(
                refreshed, request.data_type, refreshed_selection
            )
    return _accepted_response(job_repository.get(job_id) or record)


async def _invoke_option_launcher(
    launcher: EnsureJobLauncher,
    request: OptionEnsureRequest,
    *,
    job_id: str,
    gaps: tuple[SessionCoverage, ...],
    symbols: tuple[str, ...],
    timeout: float,
    on_complete: Callable[[Exception | None], None] | None = None,
) -> bool:
    """Invoke the existing launcher with the option request as its job input."""
    if not symbols:
        if on_complete is not None:
            on_complete(None)
        return True
    completed = Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            for symbol in symbols:
                job = EnsureJob(
                    job_id=f"{job_id}-{symbol}",
                    request=EnsureSpec(
                        asset_class=request.asset_class,
                        data_type=request.data_type,
                        symbol=symbol,
                        start_date=request.start_date,
                        end_date=request.end_date,
                    ),
                    gaps=tuple(gap for gap in gaps if gap.symbol == symbol),
                )
                result = launcher(job)
                if inspect.isawaitable(result):
                    asyncio.run(cast(Coroutine[Any, Any, None], result))
        except Exception as error:
            errors.append(error)
        finally:
            try:
                if on_complete is not None:
                    on_complete(errors[0] if errors else None)
            finally:
                completed.set()

    Thread(target=run, daemon=True).start()
    if not await asyncio.to_thread(completed.wait, timeout):
        return False
    if errors:
        raise errors[0]
    return True


def _option_job_id(request: OptionEnsureRequest, symbols: tuple[str, ...]) -> str:
    """Return a stable id that includes the selected OSI membership."""
    payload = {
        "asset_class": request.asset_class.value,
        "data_type": request.data_type.value,
        "symbol": (request.symbol or "").strip().upper(),
        "underlying": (request.underlying or "").strip().upper(),
        "contracts": sorted(symbols),
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return f"ensure-{hashlib.sha256(encoded).hexdigest()}"


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
