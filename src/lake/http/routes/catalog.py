"""Authenticated catalog coverage routes for the market-lake API."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, model_validator

from lake.catalog.front_sets import DailyFrontSetRepository
from lake.catalog.memory import InMemoryCatalogRepository
from lake.catalog.models import ContractRecord, CoverageRecord, CoverageStatus
from lake.catalog.ports import CatalogRepository, ContractRepository
from lake.contracts import AssetClass, DataType
from lake.coverage import (
    CoverageReport,
    CoverageSession,
    CoverageState,
    SessionCoverage,
    sessions_for,
)
from lake.coverage.application import (
    CoverageRequestError,
    EnsureSpec,
    requested_coverage,
)
from lake.http.models import CoverageRequest, StrictModel, Symbol


class CatalogCoverageRequest(CoverageRequest):
    """Strict query parameters for one symbol's calendar coverage report."""

    symbol: Symbol | None = None
    underlying: Symbol | None = None
    contract_symbols: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    contracts: tuple[Symbol, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def _validate_option_selection(self) -> CatalogCoverageRequest:
        if self.asset_class is not AssetClass.OPTION and self.symbol is None:
            raise ValueError("symbol is required for equity and crypto coverage")
        if (
            self.contract_symbols is not None or self.contracts is not None
        ) and self.asset_class is not AssetClass.OPTION:
            raise ValueError("contract_symbols are supported for options only")
        if self.underlying is not None and self.asset_class is not AssetClass.OPTION:
            raise ValueError("underlying is supported for options only")
        if (
            self.asset_class is AssetClass.OPTION
            and self.symbol is None
            and self.underlying is None
            and self.contract_symbols is None
            and self.contracts is None
        ):
            raise ValueError("an option underlying or selected contract is required")
        return self


class OptionRequestLike(Protocol):
    """Common fields used by the route-local option coverage planner."""

    @property
    def asset_class(self) -> AssetClass: ...

    @property
    def data_type(self) -> DataType: ...

    @property
    def symbol(self) -> str | None: ...

    @property
    def underlying(self) -> str | None: ...

    @property
    def contract_symbols(self) -> tuple[str, ...] | None: ...

    @property
    def contracts(self) -> tuple[str, ...] | None: ...

    @property
    def start_date(self) -> date: ...

    @property
    def end_date(self) -> date: ...


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


class OptionCoverageEntryResponse(CoverageEntryResponse):
    """Coverage metadata for one selected OSI contract and session."""

    contract_symbol: str


class OptionCatalogCoverageResponse(StrictModel):
    """Calendar-aware coverage for selected option contracts."""

    asset_class: Literal[AssetClass.OPTION]
    data_type: DataType
    underlying: str | None = None
    contracts: tuple[str, ...]
    start_date: date
    end_date: date
    entries: tuple[OptionCoverageEntryResponse, ...]


def get_catalog_repository(request: Request) -> CatalogRepository:
    """Resolve the repository from application state for testable wiring."""
    repository = getattr(request.app.state, "catalog_repository", None)
    if repository is None:
        # This fallback keeps a directly mounted router deterministic while the
        # application factory supplies the normal dependency.
        repository = InMemoryCatalogRepository()
        request.app.state.catalog_repository = repository
    return cast(CatalogRepository, repository)


def get_option_contract_repository(request: Request) -> ContractRepository | None:
    """Resolve optional T024 option contract metadata from application state."""
    repository = getattr(request.app.state, "contract_repository", None)
    return cast(ContractRepository | None, repository)


def get_option_front_set_repository(
    request: Request,
) -> DailyFrontSetRepository | None:
    """Resolve optional date-specific option membership from application state."""
    repository = getattr(request.app.state, "front_set_repository", None)
    return cast(DailyFrontSetRepository | None, repository)


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
        response_model=CatalogCoverageResponse | OptionCatalogCoverageResponse,
        responses={422: {"description": "Invalid coverage request"}},
    )
    async def coverage(
        request: Annotated[CatalogCoverageRequest, Query()],
        repository: Annotated[CatalogRepository, Depends(get_catalog_repository)],
        contract_repository: Annotated[
            ContractRepository | None, Depends(get_option_contract_repository)
        ],
        front_set_repository: Annotated[
            DailyFrontSetRepository | None, Depends(get_option_front_set_repository)
        ],
        max_range_days: Annotated[int, Depends(get_catalog_max_range_days)],
        as_of: Annotated[datetime, Depends(get_coverage_as_of)],
    ) -> CatalogCoverageResponse | OptionCatalogCoverageResponse:
        """Return computed coverage statuses without starting ingest work."""
        try:
            if request.asset_class is AssetClass.OPTION:
                report, selection = option_coverage(
                    repository,
                    request,
                    contract_repository=contract_repository,
                    front_set_repository=front_set_repository,
                    max_range_days=max_range_days,
                    as_of=as_of,
                )
                return option_coverage_response(report, request.data_type, selection)
            report = requested_coverage(
                _FilteredCatalogRepository(
                    repository=repository,
                    asset_class=request.asset_class,
                    data_type=request.data_type,
                    symbol=cast(str, request.symbol),
                ),
                EnsureSpec(
                    asset_class=request.asset_class,
                    data_type=request.data_type,
                    symbol=cast(str, request.symbol),
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
            symbol=cast(str, request.symbol).strip().upper(),
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


@dataclass(frozen=True, slots=True)
class OptionRequestSelection:
    """Canonical option membership resolved for a coverage request."""

    underlying: str | None
    symbols_by_date: dict[date, tuple[str, ...]]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    symbol
                    for values in self.symbols_by_date.values()
                    for symbol in values
                }
            )
        )


def option_coverage(
    catalog: CatalogRepository,
    request: OptionRequestLike,
    *,
    contract_repository: ContractRepository | None,
    front_set_repository: DailyFrontSetRepository | None,
    max_range_days: int,
    as_of: datetime,
) -> tuple[CoverageReport, OptionRequestSelection]:
    """Calculate option coverage against XNYS sessions and OSI partitions."""
    start = request.start_date
    end = request.end_date
    requested_days = (end - start).days + 1
    if requested_days > max_range_days:
        raise CoverageRequestError(
            "range_too_large",
            "requested date range exceeds the catalog limit",
            details={
                "max_range_days": max_range_days,
                "requested_range_days": requested_days,
            },
        )
    sessions = sessions_for(AssetClass.EQUITY, start, end, as_of=as_of)
    selection = resolve_option_selection(
        request,
        session_dates=tuple(session.session_date for session in sessions),
        contract_repository=contract_repository,
        front_set_repository=front_set_repository,
    )
    data_type = DataType(request.data_type)
    entries = tuple(
        _option_entry(catalog, data_type, symbol, session)
        for session in sessions
        for symbol in selection.symbols_by_date[session.session_date]
    )
    return CoverageReport(AssetClass.OPTION, start, end, entries), selection


def resolve_option_selection(
    request: OptionRequestLike,
    *,
    session_dates: Iterable[date],
    contract_repository: ContractRepository | None,
    front_set_repository: DailyFrontSetRepository | None,
) -> OptionRequestSelection:
    """Resolve explicit OSI symbols or persisted front-set membership."""
    dates = tuple(session_dates)
    underlying = _canonical_optional(request.underlying)
    symbol = _canonical_optional(request.symbol)
    requested_symbols = request.contract_symbols
    aliases = request.contracts
    if requested_symbols is not None and aliases is not None:
        raise CoverageRequestError(
            "validation_error",
            "use only one of contract_symbols or contracts",
        )
    requested_symbols = requested_symbols or aliases
    symbols = (
        tuple(_canonical(value) for value in requested_symbols)
        if requested_symbols is not None
        else ()
    )
    if not symbols and symbol is not None:
        contract = contract_repository.get(symbol) if contract_repository else None
        if contract is not None and (
            underlying is None or contract.underlying == underlying
        ):
            symbols = (contract.symbol,)
            underlying = contract.underlying
        elif underlying is None and contract_repository is not None:
            candidates = contract_repository.list(underlying=symbol)
            if candidates:
                underlying = symbol
            else:
                if (
                    not _looks_like_option_symbol(symbol)
                    and front_set_repository is None
                ):
                    raise CoverageRequestError(
                        "unsupported_asset_class",
                        "option symbol must identify an OSI contract",
                    )
                if _looks_like_option_symbol(symbol):
                    symbols = (symbol,)
                else:
                    underlying = symbol
        else:
            if (
                underlying is None
                and front_set_repository is None
                and not _looks_like_option_symbol(symbol)
            ):
                raise CoverageRequestError(
                    "unsupported_asset_class",
                    "option symbol must identify an OSI contract",
                )
            if underlying is None:
                if _looks_like_option_symbol(symbol):
                    symbols = (symbol,)
                else:
                    underlying = symbol
            else:
                symbols = (symbol,)

    if symbols:
        records = _validate_contracts(symbols, underlying, contract_repository)
        if underlying is None and records:
            known_underlyings = {record.underlying for record in records}
            if len(known_underlyings) == 1:
                underlying = next(iter(known_underlyings))
        return OptionRequestSelection(
            underlying=underlying,
            symbols_by_date={
                session_date: tuple(sorted(set(symbols))) for session_date in dates
            },
        )
    if underlying is None:
        raise CoverageRequestError(
            "option_selection_unavailable",
            "an option underlying or selected contract is required",
        )

    if contract_repository is None and front_set_repository is None:
        raise CoverageRequestError(
            "option_selection_unavailable",
            "option contract metadata or a persisted front set is required",
        )

    selected_by_date: dict[date, tuple[str, ...]] = {}
    for session_date in dates:
        front_set = (
            front_set_repository.get(underlying, session_date)
            if front_set_repository is not None
            else None
        )
        fallback = (
            tuple(
                contract.symbol
                for contract in contract_repository.list(
                    underlying=underlying, as_of=session_date
                )
            )
            if contract_repository is not None
            else ()
        )
        selected_by_date[session_date] = tuple(
            sorted(front_set.symbols if front_set is not None else fallback)
        )
    if dates and not any(selected_by_date.values()):
        raise CoverageRequestError(
            "option_selection_unavailable",
            "option contract metadata or a persisted front set is required",
        )
    return OptionRequestSelection(underlying, selected_by_date)


def option_coverage_response(
    report: CoverageReport,
    data_type: DataType,
    selection: OptionRequestSelection,
) -> OptionCatalogCoverageResponse:
    """Render the shared coverage states with the OSI symbol on every entry."""
    return OptionCatalogCoverageResponse(
        asset_class=AssetClass.OPTION,
        data_type=data_type,
        underlying=selection.underlying,
        contracts=selection.symbols,
        start_date=report.start,
        end_date=report.end,
        entries=tuple(
            OptionCoverageEntryResponse(
                contract_symbol=entry.symbol,
                session_date=entry.session_date,
                open_time=entry.session.open_time,
                close_time=entry.session.close_time,
                is_partial=entry.session.is_partial,
                status=entry.state,
                object_path=None if entry.record is None else entry.record.object_path,
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


def _option_entry(
    catalog: CatalogRepository,
    data_type: DataType,
    symbol: str,
    session: CoverageSession,
) -> SessionCoverage:
    session_date = session.session_date
    record = catalog.get(AssetClass.OPTION, data_type, symbol, session_date)
    if record is not None and record.status is CoverageStatus.EMPTY:
        state = CoverageState.EMPTY
    elif session.is_partial:
        state = CoverageState.PARTIAL
    elif record is None:
        state = CoverageState.MISSING
    else:
        state = CoverageState(record.status.value)
    return SessionCoverage(
        asset_class=AssetClass.OPTION,
        data_type=data_type,
        symbol=symbol,
        session=session,
        state=state,
        record=record,
    )


def _validate_contracts(
    symbols: tuple[str, ...],
    underlying: str | None,
    repository: ContractRepository | None,
) -> tuple[ContractRecord, ...]:
    if repository is None:
        return ()
    records: list[ContractRecord] = []
    for symbol in symbols:
        record = repository.get(symbol)
        if record is None:
            raise CoverageRequestError(
                "option_contract_not_found",
                f"selected option contract does not exist: {symbol}",
            )
        records.append(record)
    known_underlyings = {record.underlying for record in records}
    if underlying is not None and known_underlyings != {underlying}:
        raise CoverageRequestError(
            "option_underlying_mismatch",
            "selected option contracts must share the requested underlying",
        )
    return tuple(records)


def _canonical(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageRequestError(
            "validation_error", "option symbols must be non-empty"
        )
    return value.strip().upper()


def _canonical_optional(value: object) -> str | None:
    return None if value is None else _canonical(value)


def _looks_like_option_symbol(symbol: str) -> bool:
    """Keep the legacy single-symbol spelling reserved for OSI contracts."""
    return len(symbol) >= 18 and symbol[-9] in {"C", "P"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CatalogCoverageRequest",
    "CatalogCoverageResponse",
    "CoverageEntryResponse",
    "OptionCatalogCoverageResponse",
    "OptionCoverageEntryResponse",
    "OptionRequestLike",
    "OptionRequestSelection",
    "create_catalog_router",
    "get_catalog_max_range_days",
    "get_catalog_repository",
    "get_option_contract_repository",
    "get_option_front_set_repository",
    "get_coverage_as_of",
    "option_coverage",
    "option_coverage_response",
    "resolve_option_selection",
]
