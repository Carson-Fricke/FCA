"""Authenticated catalog coverage routes for the market-lake API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from lake.catalog.memory import InMemoryCatalogRepository
from lake.catalog.models import CoverageRecord
from lake.catalog.ports import CatalogRepository
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageState
from lake.coverage.application import (
    CoverageRequestError,
    EnsureSpec,
    requested_coverage,
)
from lake.http.models import CoverageRequest, StrictModel, Symbol


class CatalogCoverageRequest(CoverageRequest):
    """Strict query parameters for one symbol's calendar coverage report."""

    symbol: Symbol


class CoverageEntryResponse(StrictModel):
    """Calculated status and catalog metadata for one expected session."""

    session_date: date
    open_time: datetime
    close_time: datetime
    is_partial: bool
    status: CoverageState
    object_path: str | None = None
    row_count: int | None = None
    byte_size: int | None = None
    checksum: str | None = None
    error_message: str | None = None


class CatalogCoverageResponse(StrictModel):
    """Calendar-aware coverage for one symbol, data type, and date range."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    start_date: date
    end_date: date
    entries: tuple[CoverageEntryResponse, ...]


def get_catalog_repository(request: Request) -> CatalogRepository:
    """Resolve the repository from application state for testable wiring."""
    repository = getattr(request.app.state, "catalog_repository", None)
    if repository is None:
        # This fallback keeps a directly mounted router deterministic while the
        # application factory supplies the normal dependency.
        repository = InMemoryCatalogRepository()
        request.app.state.catalog_repository = repository
    return cast(CatalogRepository, repository)


def get_catalog_max_range_days(request: Request) -> int:
    """Resolve the configured inclusive date-range limit."""
    value = getattr(request.app.state, "catalog_max_range_days", 366)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeError("catalog_max_range_days must be a positive integer")
    return value


def get_coverage_as_of(request: Request) -> datetime:
    """Resolve the clock used to identify a currently open session."""
    clock = getattr(request.app.state, "coverage_clock", _utc_now)
    value = cast(Callable[[], datetime], clock)()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("coverage_clock must return a timezone-aware datetime")
    return value


@dataclass(frozen=True, slots=True)
class _FilteredCatalogRepository:
    """Apply the route's complete partition filters before catalog scanning."""

    repository: CatalogRepository
    asset_class: AssetClass
    data_type: DataType
    symbol: str

    def get(
        self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
    ) -> CoverageRecord | None:
        return self.repository.get(asset_class, data_type, symbol, session_date)

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        return self.repository.upsert(record)

    def list(
        self,
        *,
        asset_class: AssetClass | None = None,
        data_type: DataType | None = None,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CoverageRecord, ...]:
        del asset_class, data_type, symbol
        return self.repository.list(
            asset_class=self.asset_class,
            data_type=self.data_type,
            symbol=self.symbol,
            start=start,
            end=end,
        )


def create_catalog_router() -> APIRouter:
    """Create the catalog router without binding it to a storage implementation."""
    router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

    @router.get(
        "/coverage",
        response_model=CatalogCoverageResponse,
        responses={422: {"description": "Invalid coverage request"}},
    )
    async def coverage(
        request: Annotated[CatalogCoverageRequest, Query()],
        repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
        max_range_days: Annotated[int, Depends(get_catalog_max_range_days)],
        as_of: Annotated[datetime, Depends(get_coverage_as_of)],
    ) -> CatalogCoverageResponse:
        """Return computed coverage statuses without starting ingest work."""
        try:
            report = requested_coverage(
                _FilteredCatalogRepository(
                    repository=repository,
                    asset_class=request.asset_class,
                    data_type=request.data_type,
                    symbol=request.symbol,
                ),
                EnsureSpec(
                    asset_class=request.asset_class,
                    data_type=request.data_type,
                    symbol=request.symbol,
                    start_date=request.start_date,
                    end_date=request.end_date,
                ),
                max_range_days=max_range_days,
                as_of=as_of,
                operation="catalog",
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
                    object_path=(
                        None if entry.record is None else entry.record.object_path
                    ),
                    row_count=None if entry.record is None else entry.record.row_count,
                    byte_size=None if entry.record is None else entry.record.byte_size,
                    checksum=None if entry.record is None else entry.record.checksum,
                    error_message=None
                    if entry.record is None
                    else entry.record.error_message,
                )
                for entry in report.entries
            ),
        )

    return router


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CatalogCoverageRequest",
    "CatalogCoverageResponse",
    "CoverageEntryResponse",
    "create_catalog_router",
    "get_catalog_max_range_days",
    "get_catalog_repository",
    "get_coverage_as_of",
]
