"""Pure planning for completed-session equity catch-up work.

The planner only reads coverage metadata and creates deterministic work
descriptors.  Fetching data and publishing objects remain responsibilities of
the injected queue worker and the equity ingest transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from lake.catalog import CatalogRepository
from lake.coverage import (
    CoverageSession,
    CoverageState,
    SessionCoverage,
    calculate_coverage,
    equity_sessions,
)
from lake.partition_paths import AssetClass, DataType
from lake.settings import BackfillSettings
from lake.storage import QueueSender
from lake.universes import EquityUniverse


class CatchUpOrder(StrEnum):
    """Supported chronological orderings for catch-up work."""

    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"


class _HasOrder(Protocol):
    order: str


OrderValue = Literal["newest_first", "oldest_first"]
_ACTIONABLE_STATES = frozenset(
    {CoverageState.MISSING, CoverageState.PARTIAL, CoverageState.ERROR}
)
_SESSION_LOOKBACK = timedelta(days=31)


@dataclass(frozen=True, slots=True)
class EquityCatchUpItem:
    """One symbol-session request for the T034 transaction.

    A request is grouped by symbol and session because T034 fetches and
    publishes both equity data types in one atomic transaction.  ``gaps``
    contains only the non-terminal data types that caused the request.
    """

    symbol: str
    session: CoverageSession
    gaps: tuple[SessionCoverage, ...]

    @property
    def session_date(self) -> date:
        """Return the XNYS session date represented by this work item."""
        return self.session.session_date

    @property
    def data_types(self) -> tuple[DataType, ...]:
        """Return gap data types in stable enum-value order."""
        return tuple(
            sorted({gap.data_type for gap in self.gaps}, key=lambda item: item.value)
        )

    @property
    def statuses(self) -> tuple[CoverageState, ...]:
        """Return distinct gap states in stable enum-value order."""
        return tuple(
            sorted({gap.state for gap in self.gaps}, key=lambda item: item.value)
        )

    @property
    def replace_existing(self) -> bool:
        """Whether any existing partial partition will be replaced."""
        return any(gap.replace_existing for gap in self.gaps)

    def queue_message(self) -> dict[str, object]:
        """Return the provider-independent message consumed by a queue worker."""
        return {
            "asset_class": AssetClass.EQUITY.value,
            "data_types": [data_type.value for data_type in self.data_types],
            "end_time": self.session.close_time.isoformat().replace("+00:00", "Z"),
            "replace_existing": self.replace_existing,
            "session_date": self.session_date.isoformat(),
            "start_time": self.session.open_time.isoformat().replace("+00:00", "Z"),
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class EquityCatchUpPlan:
    """Deterministically ordered completed-session catch-up work."""

    start: date
    end: date
    order: CatchUpOrder
    items: tuple[EquityCatchUpItem, ...]
    target_session: CoverageSession | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", CatchUpOrder(self.order))
        if self.start > self.end:
            raise ValueError("start must be on or before end")

    def __iter__(self) -> Iterator[EquityCatchUpItem]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def gaps(self) -> tuple[SessionCoverage, ...]:
        """Return all selected data-type gaps in item order."""
        return tuple(gap for item in self.items for gap in item.gaps)

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Return distinct selected session dates in plan order."""
        return tuple(dict.fromkeys(item.session_date for item in self.items))


def plan_equity_catch_up(
    catalog: CatalogRepository,
    *,
    symbols: Iterable[str] | str | None = None,
    data_types: Iterable[DataType | str] | DataType | str | None = None,
    universe: EquityUniverse | None = None,
    backfill_settings: _HasOrder | BackfillSettings | None = None,
    order: CatchUpOrder | OrderValue | str | None = None,
    as_of: datetime | None = None,
    start: date | None = None,
    end: date | None = None,
) -> EquityCatchUpPlan:
    """Plan completed equity gaps for a configured watchlist.

    With no explicit range, only the latest XNYS session already closed at
    ``as_of`` is considered.  Supplying ``start`` and ``end`` permits a
    bounded historical catch-up while retaining the same completed-session and
    terminal-state rules.  ``oldest_first`` and ``newest_first`` affect only
    session ordering; symbols and data types remain ascending tie-breakers.
    """
    current_time = _normalize_as_of(as_of)
    selected_symbols, selected_data_types, configured_order = _configuration(
        symbols=symbols,
        data_types=data_types,
        universe=universe,
        backfill_settings=backfill_settings,
        order=order,
    )

    target_session: CoverageSession | None = None
    if start is None and end is None:
        target_session = _latest_completed_session(current_time)
        if target_session is None:
            empty_date = current_time.date()
            return EquityCatchUpPlan(
                empty_date,
                empty_date,
                configured_order,
                (),
            )
        start = end = target_session.session_date
    elif start is None or end is None:
        raise ValueError("start and end must be supplied together")

    report = calculate_coverage(
        catalog,
        asset_class=AssetClass.EQUITY,
        symbols=selected_symbols,
        data_types=selected_data_types,
        start=start,
        end=end,
        as_of=current_time,
    )
    items = _group_completed_gaps(report.entries, current_time, configured_order)
    return EquityCatchUpPlan(
        start,
        end,
        configured_order,
        items,
        target_session,
    )


def enqueue_equity_catch_up(
    plan: EquityCatchUpPlan,
    sender: QueueSender,
) -> int:
    """Send planned messages through an injected queue boundary.

    This function has no Azure or provider behavior of its own.  Passing a
    queue sender is explicit so planning tests can remain entirely in memory.
    """
    for item in plan:
        sender.send(item.queue_message())
    return len(plan)


def _configuration(
    *,
    symbols: Iterable[str] | str | None,
    data_types: Iterable[DataType | str] | DataType | str | None,
    universe: EquityUniverse | None,
    backfill_settings: _HasOrder | BackfillSettings | None,
    order: CatchUpOrder | OrderValue | str | None,
) -> tuple[
    Iterable[str] | str,
    Iterable[DataType | str] | DataType | str,
    CatchUpOrder,
]:
    if symbols is None and universe is not None:
        symbols = universe.symbols
    if data_types is None and universe is not None:
        data_types = universe.data_types
    if symbols is None:
        raise ValueError("symbols or universe must be supplied")
    if data_types is None:
        data_types = (DataType.TRADE, DataType.QUOTE)

    configured_value: str | None = order
    if configured_value is None and backfill_settings is not None:
        configured_value = backfill_settings.order
    if configured_value is None and universe is not None:
        configured_value = universe.backfill_order
    if configured_value is None:
        configured_value = CatchUpOrder.NEWEST_FIRST.value
    try:
        configured_order = CatchUpOrder(configured_value)
    except ValueError as error:
        raise ValueError("order must be 'newest_first' or 'oldest_first'") from error
    return symbols, data_types, configured_order


def _latest_completed_session(as_of: datetime) -> CoverageSession | None:
    sessions = equity_sessions(
        as_of.date() - _SESSION_LOOKBACK,
        as_of.date(),
        as_of=as_of,
    )
    completed = [session for session in sessions if session.close_time <= as_of]
    if not completed:
        return None
    return max(completed, key=lambda session: session.session_date)


def _group_completed_gaps(
    entries: Iterable[SessionCoverage],
    as_of: datetime,
    order: CatchUpOrder,
) -> tuple[EquityCatchUpItem, ...]:
    grouped: dict[tuple[str, date], list[SessionCoverage]] = {}
    sessions: dict[tuple[str, date], CoverageSession] = {}
    for entry in entries:
        # The close-time check is intentional in addition to is_partial: T025
        # classifies a future missing session as missing, but it is not complete.
        if entry.session.close_time > as_of or entry.state not in _ACTIONABLE_STATES:
            continue
        key = (entry.symbol, entry.session_date)
        grouped.setdefault(key, []).append(entry)
        sessions[key] = entry.session

    items = tuple(
        EquityCatchUpItem(
            symbol=symbol,
            session=sessions[(symbol, session_date)],
            gaps=tuple(
                sorted(
                    gaps,
                    key=lambda gap: (gap.data_type.value, gap.state.value),
                )
            ),
        )
        for symbol, session_date in sorted(
            grouped,
            key=lambda key: _item_sort_key(key, order),
        )
        for gaps in (grouped[(symbol, session_date)],)
    )
    return items


def _item_sort_key(
    key: tuple[str, date],
    order: CatchUpOrder,
) -> tuple[int, str]:
    symbol, session_date = key
    session_order = session_date.toordinal()
    if order is CatchUpOrder.NEWEST_FIRST:
        session_order = -session_order
    return session_order, symbol


def _normalize_as_of(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "CatchUpOrder",
    "EquityCatchUpItem",
    "EquityCatchUpPlan",
    "enqueue_equity_catch_up",
    "plan_equity_catch_up",
]
