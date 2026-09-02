"""Authenticated file-query routes for the market-lake API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from lake.catalog.ports import CatalogRepository
from lake.contracts import AssetClass, DataType
from lake.coverage import SessionCoverage
from lake.coverage.application import CoverageRequestError, EnsureSpec
from lake.http.models import EnsureRequest, StrictModel
from lake.http.routes.catalog import (
    get_catalog_max_range_days,
    get_catalog_repository,
    get_coverage_as_of,
)
from lake.query import (
    BoundedDuckDBQuery,
    BoundedQueryError,
    BoundedQueryResult,
    ForceOverrideNotAuthorizedError,
    ResultLimitExceededError,
    ScanLimitExceededError,
    SqlNotAllowedError,
    StoragePathNotAllowedError,
)
from lake.query.application import (
    QueryApplicationError,
    QuerySpec,
    SqlQueryResult,
    execute_query,
)
from lake.storage.ports import SasIssuer


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
        spec = _query_spec(request)
        callback = None
        if ensure_callback is not None:

            def callback(value: EnsureSpec) -> None | Awaitable[None]:
                return ensure_callback(_ensure_request(value))

        try:
            result = await execute_query(
                spec,
                catalog,
                max_range_days=max_range_days,
                as_of=as_of,
                ensure_callback=callback,
                bounded_query=bounded_query,
                force_authorized=force_authorized,
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
        except BoundedQueryError as error:
            raise _bounded_query_error(error) from error
        except QueryApplicationError as error:
            raise _application_error(error) from error

        if isinstance(result, SqlQueryResult):
            return _sql_response(result.result, result.output)
        if sas_issuer is None and result.entries:
            raise _sas_unavailable_error()
        response = _build_response(
            result.entries,
            requested_sessions=len(result.report.entries),
            pending_sessions=result.pending_sessions,
            sas_issuer=sas_issuer,
            ensure_pending=result.pending_sessions > 0,
        )
        if result.pending_sessions:
            return JSONResponse(
                status_code=202,
                content=response.model_dump(mode="json"),
            )
        return response

    return router


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


def _query_spec(request: FileQueryRequest | SqlQueryRequest) -> QuerySpec:
    return QuerySpec(
        asset_class=request.asset_class,
        data_type=request.data_type,
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
        mode=request.mode,
        ensure=request.ensure,
        sql=request.sql if isinstance(request, SqlQueryRequest) else None,
        output=request.output if isinstance(request, SqlQueryRequest) else "json",
        force=request.force if isinstance(request, SqlQueryRequest) else False,
    )


def _ensure_request(request: EnsureSpec) -> EnsureRequest:
    return EnsureRequest(
        asset_class=request.asset_class,
        data_type=request.data_type,
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
    )


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


def _application_error(error: QueryApplicationError) -> HTTPException:
    status_code = {
        "coverage_incomplete": 409,
        "ensure_failed": 502,
        "ensure_unavailable": 503,
        "query_unavailable": 503,
    }.get(error.code, 422)
    return HTTPException(
        status_code=status_code,
        detail={
            "code": error.code,
            "message": str(error),
            **({"details": error.details} if error.details is not None else {}),
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
