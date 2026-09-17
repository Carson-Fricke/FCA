"""Provider-independent orchestration for file and SQL queries."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from lake.catalog.models import CoverageRecord
from lake.catalog.ports import CatalogRepository
from lake.contracts import AssetClass, DataType, partition_path
from lake.coverage import CoverageReport, CoverageState, SessionCoverage
from lake.coverage.application import EnsureSpec, requested_coverage
from lake.query.service import BoundedDuckDBQuery, BoundedQueryResult, EnsuredQueryFile

EnsureCallback = Callable[[EnsureSpec], None | Awaitable[None]]


class QueryApplicationError(RuntimeError):
    """A stable application error translated by the HTTP boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        self.code = code
        self.details = details
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class QuerySpec(EnsureSpec):
    mode: Literal["file", "sql"]
    ensure: bool = False
    sql: str | None = None
    output: Literal["json", "arrow"] = "json"
    force: bool = False


@dataclass(frozen=True, slots=True)
class FileQueryResult:
    report: CoverageReport
    entries: tuple[SessionCoverage, ...]
    pending_sessions: int


@dataclass(frozen=True, slots=True)
class SqlQueryResult:
    result: BoundedQueryResult
    output: Literal["json", "arrow"]


@dataclass(frozen=True, slots=True)
class _RequestedCatalogRepository:
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


async def execute_query(
    spec: QuerySpec,
    catalog: CatalogRepository,
    *,
    as_of: datetime,
    ensure_callback: EnsureCallback | None,
    bounded_query: BoundedDuckDBQuery | None,
    force_authorized: bool,
) -> FileQueryResult | SqlQueryResult:
    """Ensure, classify, and execute one file or bounded SQL query."""
    report = _coverage_report(spec, catalog, as_of)
    usable, incomplete = _classify_files(report, spec)
    if incomplete and not spec.ensure:
        raise _incomplete_error(incomplete, ensure_requested=False)

    if incomplete:
        if ensure_callback is None:
            raise QueryApplicationError(
                "ensure_unavailable", "coverage ensure is not configured"
            )
        try:
            callback_result = ensure_callback(
                EnsureSpec(
                    asset_class=spec.asset_class,
                    data_type=spec.data_type,
                    symbol=spec.symbol,
                    start_date=spec.start_date,
                    end_date=spec.end_date,
                )
            )
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception as error:
            raise QueryApplicationError(
                "ensure_failed",
                "coverage ensure failed",
                details={"error": str(error)},
            ) from error
        report = _coverage_report(spec, catalog, as_of)
        usable, incomplete = _classify_files(report, spec)
        if incomplete:
            if spec.mode == "sql":
                raise _incomplete_error(incomplete, ensure_requested=True)
            return FileQueryResult(report, usable, len(incomplete))

    if spec.mode == "file":
        return FileQueryResult(report, usable, 0)
    if bounded_query is None:
        raise QueryApplicationError(
            "query_unavailable", "bounded SQL query storage is not configured"
        )
    if spec.sql is None:
        raise ValueError("SQL mode requires a statement")
    return SqlQueryResult(
        bounded_query.execute(
            spec.sql,
            _ensured_files(usable, spec),
            force=spec.force,
            force_authorized=force_authorized,
            output=spec.output,
        ),
        spec.output,
    )


def _coverage_report(
    spec: QuerySpec,
    catalog: CatalogRepository,
    as_of: datetime,
) -> CoverageReport:
    return requested_coverage(
        _RequestedCatalogRepository(
            repository=catalog,
            asset_class=spec.asset_class,
            data_type=spec.data_type,
            symbol=spec.symbol,
        ),
        spec,
        as_of=as_of,
        operation="query",
    )


def _classify_files(
    report: CoverageReport,
    spec: QuerySpec,
) -> tuple[tuple[SessionCoverage, ...], tuple[SessionCoverage, ...]]:
    usable: list[SessionCoverage] = []
    incomplete: list[SessionCoverage] = []
    for entry in report.entries:
        if entry.state is not CoverageState.COMPLETE or not _has_expected_path(
            entry, spec
        ):
            incomplete.append(entry)
        else:
            usable.append(entry)
    return tuple(usable), tuple(incomplete)


def _has_expected_path(entry: SessionCoverage, spec: QuerySpec) -> bool:
    record = entry.record
    if record is None or record.object_path is None:
        return False
    return record.object_path == partition_path(
        session_date=entry.session_date,
        asset_class=spec.asset_class,
        data_type=spec.data_type,
        symbol=spec.symbol,
    )


def _ensured_files(
    entries: tuple[SessionCoverage, ...], spec: QuerySpec
) -> tuple[EnsuredQueryFile, ...]:
    files: list[EnsuredQueryFile] = []
    for entry in entries:
        record = entry.record
        if record is None or record.object_path is None:
            raise RuntimeError("complete coverage entry has no object path")
        files.append(
            EnsuredQueryFile(
                asset_class=spec.asset_class,
                data_type=spec.data_type,
                symbol=spec.symbol,
                session_date=entry.session_date,
                object_path=record.object_path,
                byte_size=record.byte_size,
            )
        )
    return tuple(files)


def _incomplete_error(
    entries: tuple[SessionCoverage, ...], *, ensure_requested: bool
) -> QueryApplicationError:
    message = (
        "coverage remains incomplete after ensure"
        if ensure_requested
        else "requested coverage is incomplete; set ensure=true to request coverage"
    )
    return QueryApplicationError(
        "coverage_incomplete",
        message,
        details={
            "sessions": [
                {
                    "session_date": entry.session_date.isoformat(),
                    "state": entry.state,
                }
                for entry in entries
            ]
        },
    )


__all__ = [
    "EnsureCallback",
    "FileQueryResult",
    "QueryApplicationError",
    "QuerySpec",
    "SqlQueryResult",
    "execute_query",
]
