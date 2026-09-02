"""Authenticated file-query routes for the market-lake API."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from lake.bounded_query import (
    BoundedDuckDBQuery,
    BoundedQueryError,
    BoundedQueryResult,
    EnsuredQueryFile,
    ForceOverrideNotAuthorizedError,
    ResultLimitExceededError,
    ScanLimitExceededError,
    SqlNotAllowedError,
    StoragePathNotAllowedError,
)
from lake.catalog import CatalogRepository, CoverageRecord
from lake.catalog_api import (
    get_catalog_max_range_days,
    get_catalog_repository,
    get_coverage_as_of,
)
from lake.coverage import (
    CoverageReport,
    CoverageState,
    SessionCoverage,
    calculate_coverage,
)
from lake.models import EnsureRequest, StrictModel
from lake.partition_paths import AssetClass, DataType, partition_path
from lake.storage import SasIssuer


class FileQueryRequest(EnsureRequest):
    """Strict request body for the file-only query mode."""

    mode: Literal["file"]
    ensure: bool = False


class SqlQueryRequest(EnsureRequest):
    """Strict request body for bounded SQL over the ensured request files."""

    mode: Literal["sql"]
    sql: Annotated[str, Field(min_length=1, max_length=100_000)]
    output: Literal["json", "arrow"] = "json"
    ensure: bool = False
    force: bool = False


QueryRequest = Annotated[
    FileQueryRequest | SqlQueryRequest,
    Field(discriminator="mode"),
]


class FileQueryFileResponse(StrictModel):
    """One complete catalog partition and its read-only download URL."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session_date: date
    object_path: str
    url: str
    sas_url: str
    row_count: int
    byte_size: int
    checksum: str | None = None


class FileQueryResultsResponse(StrictModel):
    """Bounded summary of the requested sessions."""

    requested_sessions: int
    returned_files: int
    complete_sessions: int
    pending_sessions: int
    state: Literal["complete", "ensure_pending"]


class FileQueryResponse(StrictModel):
    """Files and catalog results returned by the file query mode."""

    files: tuple[FileQueryFileResponse, ...]
    results: FileQueryResultsResponse
    limitations: tuple[str, ...]


class SqlQueryResponse(StrictModel):
    """JSON representation of a bounded SQL result and its cost metadata."""

    format: Literal["json"]
    columns: tuple[str, ...]
    rows: list[dict[str, object]]
    result_bytes: int
    estimated_scan_bytes: int
    forced: bool
    limitations: tuple[str, ...]


EnsureCallback = Callable[[EnsureRequest], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _RequestedCatalogRepository:
    """Apply every request filter before the coverage calculator scans rows."""

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


def get_query_sas_issuer(request: Request) -> SasIssuer | None:
    """Resolve the optional SAS issuer without constructing Azure clients."""
    issuer = getattr(request.app.state, "sas_issuer", None)
    return cast(SasIssuer | None, issuer)


def get_query_ensure_callback(request: Request) -> EnsureCallback | None:
    """Resolve the optional injected coverage-ensure callback."""
    callback = getattr(request.app.state, "ensure_callback", None)
    return cast(EnsureCallback | None, callback)


def get_bounded_query(request: Request) -> BoundedDuckDBQuery | None:
    """Resolve the provider-free local DuckDB executor, when configured."""
    executor = getattr(request.app.state, "bounded_query", None)
    return cast(BoundedDuckDBQuery | None, executor)


def get_authorized_force_override(request: Request) -> bool:
    """Resolve the server-side authorization required by ``force=true``."""
    value = getattr(request.app.state, "authorized_force_override", False)
    if not isinstance(value, bool):
        raise RuntimeError("authorized_force_override must be a boolean")
    return value


def create_query_router() -> APIRouter:
    """Create the authenticated file-query route without Azure dependencies."""
    router = APIRouter(prefix="/v1", tags=["query"])

    @router.post(
        "/query",
        response_model=None,
        responses={
            202: {"model": FileQueryResponse},
            409: {"description": "Requested coverage is incomplete"},
        },
    )
    async def query_files(
        request: QueryRequest,
        catalog: Annotated[CatalogRepository, Depends(get_catalog_repository)],
        max_range_days: Annotated[int, Depends(get_catalog_max_range_days)],
        as_of: Annotated[datetime, Depends(get_coverage_as_of)],
        sas_issuer: Annotated[SasIssuer | None, Depends(get_query_sas_issuer)],
        ensure_callback: Annotated[
            EnsureCallback | None, Depends(get_query_ensure_callback)
        ],
        bounded_query: Annotated[BoundedDuckDBQuery | None, Depends(get_bounded_query)],
        force_authorized: Annotated[bool, Depends(get_authorized_force_override)],
    ) -> FileQueryResponse | SqlQueryResponse | Response | JSONResponse:
        """Return files or a bounded result over the requested complete files."""
        requested_days = (request.end_date - request.start_date).days + 1
        if requested_days > max_range_days:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "range_too_large",
                    "message": "requested date range exceeds the query limit",
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
                    "message": "file queries support equity and crypto only",
                },
            )

        report = _coverage_report(request, catalog, as_of)
        usable, incomplete = _classify_files(report, request)
        if incomplete and not request.ensure:
            raise _incomplete_error(incomplete, ensure_requested=False)

        if incomplete:
            if ensure_callback is None:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "ensure_unavailable",
                        "message": "coverage ensure is not configured",
                    },
                )
            try:
                result = ensure_callback(_ensure_request(request))
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "ensure_failed",
                        "message": "coverage ensure failed",
                        "details": {"error": str(error)},
                    },
                ) from error

            report = _coverage_report(request, catalog, as_of)
            usable, incomplete = _classify_files(report, request)
            if incomplete:
                if isinstance(request, SqlQueryRequest):
                    raise _incomplete_error(incomplete, ensure_requested=True)
                if sas_issuer is None and usable:
                    raise _sas_unavailable_error()
                response = _build_response(
                    usable,
                    requested_sessions=len(report.entries),
                    pending_sessions=len(incomplete),
                    sas_issuer=sas_issuer,
                    ensure_pending=True,
                )
                return JSONResponse(
                    status_code=202,
                    content=response.model_dump(mode="json"),
                )

        if isinstance(request, SqlQueryRequest):
            if bounded_query is None:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "query_unavailable",
                        "message": "bounded SQL query storage is not configured",
                    },
                )
            try:
                result = bounded_query.execute(
                    request.sql,
                    _ensured_files(usable, request),
                    force=request.force,
                    force_authorized=force_authorized,
                    output=request.output,
                )
            except BoundedQueryError as error:
                raise _bounded_query_error(error) from error
            return _sql_response(result, request.output)

        if sas_issuer is None:
            raise _sas_unavailable_error()
        return _build_response(
            usable,
            requested_sessions=len(report.entries),
            pending_sessions=0,
            sas_issuer=sas_issuer,
            ensure_pending=False,
        )

    return router


def _coverage_report(
    request: EnsureRequest,
    catalog: CatalogRepository,
    as_of: datetime,
) -> CoverageReport:
    return calculate_coverage(
        _RequestedCatalogRepository(
            repository=catalog,
            asset_class=request.asset_class,
            data_type=request.data_type,
            symbol=request.symbol,
        ),
        asset_class=request.asset_class,
        symbols=request.symbol,
        data_types=request.data_type,
        start=request.start_date,
        end=request.end_date,
        as_of=as_of,
    )


def _classify_files(
    report: CoverageReport,
    request: EnsureRequest,
) -> tuple[tuple[SessionCoverage, ...], tuple[SessionCoverage, ...]]:
    usable: list[SessionCoverage] = []
    incomplete: list[SessionCoverage] = []
    for entry in report.entries:
        if entry.state is not CoverageState.COMPLETE or not _has_expected_path(
            entry, request
        ):
            incomplete.append(entry)
        else:
            usable.append(entry)
    return tuple(usable), tuple(incomplete)


def _has_expected_path(entry: SessionCoverage, request: EnsureRequest) -> bool:
    record = entry.record
    if record is None or record.object_path is None:
        return False
    expected = partition_path(
        session_date=entry.session_date,
        asset_class=request.asset_class,
        data_type=request.data_type,
        symbol=request.symbol,
    )
    return record.object_path == expected


def _build_response(
    entries: tuple[SessionCoverage, ...],
    *,
    requested_sessions: int,
    pending_sessions: int,
    sas_issuer: SasIssuer | None,
    ensure_pending: bool,
) -> FileQueryResponse:
    if sas_issuer is None:
        raise _sas_unavailable_error()
    files = tuple(_file_response(entry, sas_issuer) for entry in entries)
    state: Literal["complete", "ensure_pending"] = (
        "ensure_pending" if ensure_pending else "complete"
    )
    limitations = (
        "Only complete catalog partitions are returned.",
        "URLs are read-only, object-scoped, and expire according to SAS policy.",
    )
    if ensure_pending:
        limitations += ("Coverage ensure was requested but is not complete yet.",)
    return FileQueryResponse(
        files=files,
        results=FileQueryResultsResponse(
            requested_sessions=requested_sessions,
            returned_files=len(files),
            complete_sessions=len(files),
            pending_sessions=pending_sessions,
            state=state,
        ),
        limitations=limitations,
    )


def _file_response(
    entry: SessionCoverage, sas_issuer: SasIssuer
) -> FileQueryFileResponse:
    record = entry.record
    if record is None or record.object_path is None:
        raise RuntimeError("complete coverage entry has no object path")
    url = sas_issuer.issue(record.object_path)
    return FileQueryFileResponse(
        asset_class=entry.asset_class,
        data_type=entry.data_type,
        symbol=entry.symbol,
        session_date=entry.session_date,
        object_path=record.object_path,
        url=url,
        sas_url=url,
        row_count=record.row_count,
        byte_size=record.byte_size,
        checksum=record.checksum,
    )


def _ensure_request(request: EnsureRequest) -> EnsureRequest:
    return EnsureRequest(
        asset_class=request.asset_class,
        data_type=request.data_type,
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
    )


def _ensured_files(
    entries: tuple[SessionCoverage, ...], request: EnsureRequest
) -> tuple[EnsuredQueryFile, ...]:
    files: list[EnsuredQueryFile] = []
    for entry in entries:
        record = entry.record
        if record is None or record.object_path is None:
            raise RuntimeError("complete coverage entry has no object path")
        files.append(
            EnsuredQueryFile(
                asset_class=request.asset_class,
                data_type=request.data_type,
                symbol=request.symbol,
                session_date=entry.session_date,
                object_path=record.object_path,
                byte_size=record.byte_size,
            )
        )
    return tuple(files)


def _sql_response(
    result: object, output: Literal["json", "arrow"]
) -> SqlQueryResponse | Response:
    bounded_result = cast(BoundedQueryResult, result)
    limitations = (
        "SQL is executed only against the canonical complete request partitions.",
        "Storage paths, mutating statements, and uncapped execution are rejected.",
    )
    if bounded_result.forced:
        limitations += (
            "The authorized force override permitted a scan or result above its configured cap.",
        )
    if output == "arrow":
        return Response(
            content=bounded_result.arrow_bytes,
            media_type="application/vnd.apache.arrow.stream",
            headers={
                "X-Query-Estimated-Scan-Bytes": str(
                    bounded_result.estimated_scan_bytes
                ),
                "X-Query-Result-Bytes": str(bounded_result.result_bytes),
                "X-Query-Force-Applied": str(bounded_result.forced).lower(),
            },
        )
    return SqlQueryResponse(
        format="json",
        columns=bounded_result.columns,
        rows=bounded_result.rows,
        result_bytes=bounded_result.result_bytes,
        estimated_scan_bytes=bounded_result.estimated_scan_bytes,
        forced=bounded_result.forced,
        limitations=limitations,
    )


def _bounded_query_error(error: BoundedQueryError) -> HTTPException:
    status_code = 503 if error.code == "query_storage_unavailable" else 422
    if isinstance(error, (ScanLimitExceededError, ResultLimitExceededError)):
        status_code = 413
    if isinstance(error, ForceOverrideNotAuthorizedError):
        status_code = 403
    if isinstance(error, (SqlNotAllowedError, StoragePathNotAllowedError)):
        status_code = 422
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": str(error)},
    )


def _incomplete_error(
    entries: tuple[SessionCoverage, ...], *, ensure_requested: bool
) -> HTTPException:
    message = (
        "coverage remains incomplete after ensure"
        if ensure_requested
        else "requested coverage is incomplete; set ensure=true to request coverage"
    )
    return HTTPException(
        status_code=409,
        detail={
            "code": "coverage_incomplete",
            "message": message,
            "details": {
                "sessions": [
                    {
                        "session_date": entry.session_date.isoformat(),
                        "state": entry.state,
                    }
                    for entry in entries
                ]
            },
        },
    )


def _sas_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "sas_unavailable",
            "message": "file query SAS issuance is not configured",
        },
    )


__all__ = [
    "EnsureCallback",
    "FileQueryFileResponse",
    "FileQueryRequest",
    "FileQueryResponse",
    "FileQueryResultsResponse",
    "QueryRequest",
    "SqlQueryRequest",
    "SqlQueryResponse",
    "get_authorized_force_override",
    "get_bounded_query",
    "create_query_router",
    "get_query_ensure_callback",
    "get_query_sas_issuer",
]
