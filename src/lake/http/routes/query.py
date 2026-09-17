"""Authenticated file-query routes for the market-lake API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, model_validator

from lake.catalog.front_sets import DailyFrontSetRepository
from lake.catalog.ports import CatalogRepository, ContractRepository
from lake.contracts import AssetClass, DataType, partition_path
from lake.coverage import SessionCoverage
from lake.coverage.application import CoverageRequestError, EnsureSpec
from lake.http.models import EnsureRequest, StrictModel, Symbol
from lake.http.routes.catalog import (
    get_catalog_repository,
    get_coverage_as_of,
    get_option_contract_repository,
    get_option_front_set_repository,
    option_coverage,
)
from lake.query import (
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
    symbol: Symbol | None = None
    underlying: Symbol | None = None
    contract_symbols: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    contracts: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def _validate_option_selection(self) -> FileQueryRequest:
        _validate_option_query_selection(self)
        return self


class SqlQueryRequest(EnsureRequest):
    """Strict request body for bounded SQL over the ensured request files."""

    mode: Literal["sql"]
    sql: Annotated[str, Field(min_length=1, max_length=100_000)]
    output: Literal["json", "arrow"] = "json"
    ensure: bool = False
    force: bool = False
    symbol: Symbol | None = None
    underlying: Symbol | None = None
    contract_symbols: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    contracts: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def _validate_option_selection(self) -> SqlQueryRequest:
        _validate_option_query_selection(self)
        return self


class OptionEnsureCallbackRequest(EnsureRequest):
    """Ensure callback payload retaining the selected option contract set."""

    underlying: Symbol | None = None
    contract_symbols: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    contracts: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )


def _validate_option_query_selection(
    request: FileQueryRequest | SqlQueryRequest,
) -> None:
    """Validate fields shared by equity, crypto, and option query variants."""
    asset_class = request.asset_class
    symbol = request.symbol
    underlying = request.underlying
    contract_symbols = request.contract_symbols
    contracts = request.contracts
    if asset_class is not AssetClass.OPTION:
        if symbol is None:
            raise ValueError("symbol is required for equity and crypto queries")
        if (
            underlying is not None
            or contract_symbols is not None
            or contracts is not None
        ):
            raise ValueError("option selection fields are supported for options only")
        return
    if contract_symbols is not None and contracts is not None:
        raise ValueError("use only one of contract_symbols or contracts")
    if (
        symbol is None
        and underlying is None
        and contract_symbols is None
        and contracts is None
    ):
        raise ValueError("an option underlying or selected contract is required")


def _as_option_request(request: FileQueryRequest | SqlQueryRequest) -> bool:
    """Return whether a query uses the option selection extension."""
    return request.asset_class is AssetClass.OPTION


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
        contract_repository: Annotated[
            ContractRepository | None, Depends(get_option_contract_repository)
        ],
        front_set_repository: Annotated[
            DailyFrontSetRepository | None, Depends(get_option_front_set_repository)
        ],
        as_of: Annotated[datetime, Depends(get_coverage_as_of)],
        sas_issuer: Annotated[SasIssuer | None, Depends(get_query_sas_issuer)],
        ensure_callback: Annotated[
            EnsureCallback | None, Depends(get_query_ensure_callback)
        ],
        bounded_query: Annotated[BoundedDuckDBQuery | None, Depends(get_bounded_query)],
        force_authorized: Annotated[bool, Depends(get_authorized_force_override)],
    ) -> FileQueryResponse | SqlQueryResponse | Response | JSONResponse:
        """Return files or a bounded result over the requested complete files."""
        option_request = request if request.asset_class is AssetClass.OPTION else None
        if option_request is not None:
            try:
                return await _query_options(
                    option_request,
                    catalog=catalog,
                    contract_repository=contract_repository,
                    front_set_repository=front_set_repository,
                    as_of=as_of,
                    sas_issuer=sas_issuer,
                    ensure_callback=ensure_callback,
                    bounded_query=bounded_query,
                    force_authorized=force_authorized,
                )
            except CoverageRequestError as error:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": error.code,
                        "message": str(error),
                        **(
                            {"details": error.details}
                            if error.details is not None
                            else {}
                        ),
                    },
                ) from error
            except BoundedQueryError as error:
                raise _bounded_query_error(error) from error
            except QueryApplicationError as error:
                raise _application_error(error) from error
        spec = _query_spec(request)
        callback = None
        if ensure_callback is not None:

            def callback(value: EnsureSpec) -> None | Awaitable[None]:
                return ensure_callback(_ensure_request(value))

        try:
            result = await execute_query(
                spec,
                catalog,
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


async def _query_options(
    request: FileQueryRequest | SqlQueryRequest,
    *,
    catalog: CatalogRepository,
    contract_repository: ContractRepository | None,
    front_set_repository: DailyFrontSetRepository | None,
    as_of: datetime,
    sas_issuer: SasIssuer | None,
    ensure_callback: EnsureCallback | None,
    bounded_query: BoundedDuckDBQuery | None,
    force_authorized: bool,
) -> FileQueryResponse | SqlQueryResponse | Response | JSONResponse:
    """Execute option file or SQL queries against selected OSI partitions."""
    report, selection = option_coverage(
        catalog,
        request,
        contract_repository=contract_repository,
        front_set_repository=front_set_repository,
        as_of=as_of,
    )
    usable = tuple(
        entry
        for entry in report.entries
        if entry.is_complete and _has_option_expected_path(entry, request)
    )
    incomplete = tuple(
        entry
        for entry in report.entries
        if not entry.is_complete or not _has_option_expected_path(entry, request)
    )
    if incomplete and not request.ensure:
        raise _option_incomplete_error(incomplete, ensure_requested=False)
    if incomplete and request.ensure:
        if ensure_callback is None:
            raise _application_error(
                QueryApplicationError(
                    "ensure_unavailable", "coverage ensure is not configured"
                )
            )
        callback_request = _option_callback_request(request, selection)
        try:
            callback_result = ensure_callback(callback_request)
            if hasattr(callback_result, "__await__"):
                await callback_result
        except Exception as error:
            raise _application_error(
                QueryApplicationError(
                    "ensure_failed",
                    "coverage ensure failed",
                    details={"error": str(error)},
                )
            ) from error
        report, selection = option_coverage(
            catalog,
            request,
            contract_repository=contract_repository,
            front_set_repository=front_set_repository,
            as_of=as_of,
        )
        usable = tuple(
            entry
            for entry in report.entries
            if entry.is_complete and _has_option_expected_path(entry, request)
        )
        incomplete = tuple(
            entry
            for entry in report.entries
            if not entry.is_complete or not _has_option_expected_path(entry, request)
        )
        if incomplete and request.mode == "sql":
            raise _option_incomplete_error(incomplete, ensure_requested=True)
    if request.mode == "sql":
        if bounded_query is None:
            raise _application_error(
                QueryApplicationError(
                    "query_unavailable", "bounded SQL query storage is not configured"
                )
            )
        result = bounded_query.execute(
            request.sql,
            _option_ensured_files(usable, request),
            force=request.force,
            force_authorized=force_authorized,
            output=request.output,
        )
        return _sql_response(result, request.output)
    if sas_issuer is None and usable:
        raise _sas_unavailable_error()
    response = _build_response(
        usable,
        requested_sessions=len(report.entries),
        pending_sessions=len(incomplete),
        sas_issuer=sas_issuer,
        ensure_pending=bool(incomplete),
    )
    if incomplete:
        return JSONResponse(status_code=202, content=response.model_dump(mode="json"))
    return response


def _option_callback_request(
    request: FileQueryRequest | SqlQueryRequest,
    selection: object,
) -> EnsureRequest:
    """Adapt option selection to the existing ensure callback boundary."""
    return OptionEnsureCallbackRequest(
        asset_class=AssetClass.OPTION,
        data_type=request.data_type,
        symbol=(request.underlying or request.symbol or _first_contract(request)),
        underlying=request.underlying,
        contract_symbols=request.contract_symbols or request.contracts,
        start_date=request.start_date,
        end_date=request.end_date,
    )


def _first_contract(request: FileQueryRequest | SqlQueryRequest) -> str:
    values = request.contract_symbols or request.contracts
    if not values:
        raise ValueError("an option contract selection is required")
    return values[0]


def _option_ensured_files(
    entries: tuple[SessionCoverage, ...],
    request: FileQueryRequest | SqlQueryRequest,
) -> tuple[EnsuredQueryFile, ...]:
    """Map complete option entries to the bounded executor's file contract."""
    return tuple(
        EnsuredQueryFile(
            asset_class=AssetClass.OPTION,
            data_type=request.data_type,
            symbol=entry.symbol,
            session_date=entry.session_date,
            object_path=entry.record.object_path,
            byte_size=entry.record.byte_size,
        )
        for entry in entries
        if entry.record is not None and entry.record.object_path is not None
    )


def _has_option_expected_path(
    entry: SessionCoverage,
    request: FileQueryRequest | SqlQueryRequest,
) -> bool:
    """Require complete option rows to point at their canonical OSI object."""
    record = entry.record
    return record is not None and record.object_path == partition_path(
        session_date=entry.session_date,
        asset_class=AssetClass.OPTION,
        data_type=request.data_type,
        symbol=entry.symbol,
    )


def _option_incomplete_error(
    entries: tuple[SessionCoverage, ...], *, ensure_requested: bool
) -> HTTPException:
    """Use the existing query error shape for option coverage gaps."""
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
                "contracts": [
                    {
                        "contract_symbol": entry.symbol,
                        "session_date": entry.session_date.isoformat(),
                        "state": entry.state,
                    }
                    for entry in entries
                ]
            },
        },
    )


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
        symbol=cast(str, request.symbol),
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
