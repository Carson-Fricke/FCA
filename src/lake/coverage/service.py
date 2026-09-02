"""Calendar-aware coverage calculations and gap planning.

Coverage is evaluated for the sessions produced by :mod:`lake.contracts.sessions`,
not for every calendar date.  Empty catalog outcomes are terminal, while
partial sessions remain actionable so a later write can replace the same
deterministic Parquet partition.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.catalog.ports import CatalogRepository
from lake.contracts import (
    AssetClass,
    DataType,
    crypto_utc_days,
    symbol_filename,
    xnys_sessions,
)


class CoverageState(StrEnum):
    """Classification of one expected symbol/data-type session."""

    MISSING = "missing"
    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CoverageSession:
    """A UTC session window, including whether it is currently open."""

    session_date: date
    open_time: datetime
    close_time: datetime
    is_partial: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.session_date, datetime):
            raise TypeError("session_date must be a date, not a datetime")
        if not isinstance(self.session_date, date):
            raise TypeError("session_date must be a date")
        if self.open_time.tzinfo is None or self.open_time.utcoffset() is None:
            raise ValueError("open_time must be timezone-aware")
        if self.close_time.tzinfo is None or self.close_time.utcoffset() is None:
            raise ValueError("close_time must be timezone-aware")
        open_time = self.open_time.astimezone(UTC)
        close_time = self.close_time.astimezone(UTC)
        if open_time >= close_time:
            raise ValueError("open_time must be before close_time")
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)


@dataclass(frozen=True, slots=True)
class SessionCoverage:
    """Coverage classification for one symbol, data type, and session."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session: CoverageSession
    state: CoverageState
    record: CoverageRecord | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        object.__setattr__(self, "data_type", DataType(self.data_type))
        if not isinstance(self.symbol, str):
            raise TypeError("symbol must be a string")
        canonical_symbol = self.symbol.strip().upper()
        if not canonical_symbol:
            raise ValueError("symbol must not be empty")
        symbol_filename(canonical_symbol)
        object.__setattr__(self, "symbol", canonical_symbol)
        object.__setattr__(self, "state", CoverageState(self.state))

    @property
    def session_date(self) -> date:
        """Return the date used by the deterministic catalog and object key."""
        return self.session.session_date

    @property
    def status(self) -> CoverageStatus | None:
        """Return the persisted catalog status, if a row exists."""
        return None if self.record is None else self.record.status

    @property
    def is_complete(self) -> bool:
        """Whether this session has complete, non-empty coverage."""
        return self.state is CoverageState.COMPLETE

    @property
    def is_terminal(self) -> bool:
        """Whether no ingest retry is required for this session."""
        return self.state in {CoverageState.COMPLETE, CoverageState.EMPTY}

    @property
    def is_actionable(self) -> bool:
        """Whether this entry belongs in an ingest gap plan."""
        return not self.is_terminal

    @property
    def replace_existing(self) -> bool:
        """Whether a persisted partial row should be replaced by the fetch."""
        return self.state is CoverageState.PARTIAL and self.record is not None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Deterministically ordered classifications for an expected range."""

    asset_class: AssetClass
    start: date
    end: date
    entries: tuple[SessionCoverage, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        _validate_date_range(self.start, self.end)

    def __iter__(self) -> Iterator[SessionCoverage]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def by_state(self, state: CoverageState) -> tuple[SessionCoverage, ...]:
        """Return entries in report order for one classification."""
        normalized = CoverageState(state)
        return tuple(entry for entry in self.entries if entry.state is normalized)

    @property
    def missing(self) -> tuple[SessionCoverage, ...]:
        return self.by_state(CoverageState.MISSING)

    @property
    def empty(self) -> tuple[SessionCoverage, ...]:
        return self.by_state(CoverageState.EMPTY)

    @property
    def partial(self) -> tuple[SessionCoverage, ...]:
        return self.by_state(CoverageState.PARTIAL)

    @property
    def complete(self) -> tuple[SessionCoverage, ...]:
        return self.by_state(CoverageState.COMPLETE)

    @property
    def errors(self) -> tuple[SessionCoverage, ...]:
        return self.by_state(CoverageState.ERROR)

    @property
    def is_satisfied(self) -> bool:
        """Whether all expected sessions are complete or terminally empty."""
        return all(entry.is_terminal for entry in self.entries)

    def gap_plan(self) -> GapPlan:
        """Return the actionable entries without recalculating the report."""
        return GapPlan(
            self, tuple(entry for entry in self.entries if entry.is_actionable)
        )


@dataclass(frozen=True, slots=True)
class GapPlan:
    """Actionable coverage entries for one asset class and inclusive range."""

    report: CoverageReport
    gaps: tuple[SessionCoverage, ...]

    @property
    def asset_class(self) -> AssetClass:
        return self.report.asset_class

    @property
    def start(self) -> date:
        return self.report.start

    @property
    def end(self) -> date:
        return self.report.end

    def __iter__(self) -> Iterator[SessionCoverage]:
        return iter(self.gaps)

    def __len__(self) -> int:
        return len(self.gaps)

    @property
    def missing(self) -> tuple[SessionCoverage, ...]:
        return tuple(
            entry for entry in self.gaps if entry.state is CoverageState.MISSING
        )

    @property
    def partial(self) -> tuple[SessionCoverage, ...]:
        return tuple(
            entry for entry in self.gaps if entry.state is CoverageState.PARTIAL
        )

    @property
    def errors(self) -> tuple[SessionCoverage, ...]:
        return tuple(entry for entry in self.gaps if entry.state is CoverageState.ERROR)


def sessions_for(
    asset_class: AssetClass,
    start: date,
    end: date,
    *,
    as_of: datetime | None = None,
) -> tuple[CoverageSession, ...]:
    """Return inclusive equity or crypto sessions with current-session flags."""
    asset_class = AssetClass(asset_class)
    _validate_date_range(start, end)
    current_time = _normalize_as_of(as_of)

    if asset_class is AssetClass.EQUITY:
        ranges = (
            CoverageSession(
                session_date=session.session_date,
                open_time=session.open_time,
                close_time=session.close_time,
                is_partial=session.open_time <= current_time < session.close_time,
            )
            for session in xnys_sessions(start, end)
        )
    elif asset_class is AssetClass.CRYPTO:
        ranges = (
            _crypto_session(session_date, current_time)
            for session_date in crypto_utc_days(start, end)
        )
    else:
        raise ValueError("coverage planning supports equity and crypto only")
    return tuple(ranges)


def equity_sessions(
    start: date, end: date, *, as_of: datetime | None = None
) -> tuple[CoverageSession, ...]:
    """Return XNYS coverage sessions for an inclusive range."""
    return sessions_for(AssetClass.EQUITY, start, end, as_of=as_of)


def crypto_sessions(
    start: date, end: date, *, as_of: datetime | None = None
) -> tuple[CoverageSession, ...]:
    """Return UTC-day coverage sessions for an inclusive range."""
    return sessions_for(AssetClass.CRYPTO, start, end, as_of=as_of)


def calculate_coverage(
    catalog: CatalogRepository,
    *,
    asset_class: AssetClass,
    symbols: Iterable[str] | str,
    data_types: Iterable[DataType | str] | DataType | str,
    start: date,
    end: date,
    as_of: datetime | None = None,
) -> CoverageReport:
    """Classify every expected symbol/data-type session in a calendar range.

    Missing catalog rows are ``missing`` for closed sessions and ``partial``
    for a currently open session.  ``empty`` is retained as a distinct,
    terminal result.  Existing partial and error rows remain actionable, while
    a complete row for a currently open session is deliberately reclassified as
    partial so the canonical object can be replaced after more data arrives.
    """
    asset_class = AssetClass(asset_class)
    _validate_date_range(start, end)
    normalized_symbols = _normalize_symbols(symbols)
    normalized_data_types = _normalize_data_types(data_types)
    sessions = sessions_for(asset_class, start, end, as_of=as_of)
    records = catalog.list(asset_class=asset_class, start=start, end=end)
    records_by_key = {
        (record.data_type, record.symbol, record.session_date): record
        for record in records
    }

    entries: list[SessionCoverage] = []
    for data_type in normalized_data_types:
        for symbol in normalized_symbols:
            for session in sessions:
                record = records_by_key.get((data_type, symbol, session.session_date))
                state = _coverage_state(record, session)
                entries.append(
                    SessionCoverage(
                        asset_class=asset_class,
                        data_type=data_type,
                        symbol=symbol,
                        session=session,
                        state=state,
                        record=record,
                    )
                )
    return CoverageReport(asset_class, start, end, tuple(entries))


def plan_gaps(
    catalog: CatalogRepository,
    *,
    asset_class: AssetClass,
    symbols: Iterable[str] | str,
    data_types: Iterable[DataType | str] | DataType | str,
    start: date,
    end: date,
    as_of: datetime | None = None,
) -> GapPlan:
    """Plan missing, partial, and error sessions in deterministic order."""
    return calculate_coverage(
        catalog,
        asset_class=asset_class,
        symbols=symbols,
        data_types=data_types,
        start=start,
        end=end,
        as_of=as_of,
    ).gap_plan()


# Descriptive aliases keep the per-session and per-gap concepts discoverable.
CoverageEntry = SessionCoverage
CoverageGap = SessionCoverage


def _coverage_state(
    record: CoverageRecord | None, session: CoverageSession
) -> CoverageState:
    if record is not None and record.status is CoverageStatus.EMPTY:
        return CoverageState.EMPTY
    if session.is_partial:
        return CoverageState.PARTIAL
    if record is None:
        return CoverageState.MISSING
    return CoverageState(record.status.value)


def _crypto_session(session_date: date, as_of: datetime) -> CoverageSession:
    open_time = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    close_time = open_time + timedelta(days=1)
    return CoverageSession(
        session_date=session_date,
        open_time=open_time,
        close_time=close_time,
        is_partial=open_time <= as_of < close_time,
    )


def _normalize_as_of(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_symbols(values: Iterable[str] | str) -> tuple[str, ...]:
    items = (values,) if isinstance(values, str) else values
    normalized: set[str] = set()
    for value in items:
        if not isinstance(value, str):
            raise TypeError("symbols entries must be strings")
        normalized.add(value.strip().upper())
    if not normalized or "" in normalized:
        raise ValueError("symbols must contain non-empty strings")
    for symbol in normalized:
        symbol_filename(symbol)
    return tuple(sorted(normalized))


def _normalize_data_types(
    values: Iterable[DataType | str] | DataType | str,
) -> tuple[DataType, ...]:
    items = (values,) if isinstance(values, (DataType, str)) else values
    normalized = {DataType(value) for value in items}
    if not normalized:
        raise ValueError("data_types must not be empty")
    return tuple(sorted(normalized, key=lambda value: value.value))


def _validate_date_range(start: date, end: date) -> None:
    if isinstance(start, datetime) or isinstance(end, datetime):
        raise TypeError("start and end must be dates, not datetimes")
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError("start and end must be dates")
    if start > end:
        raise ValueError("start must be on or before end")
