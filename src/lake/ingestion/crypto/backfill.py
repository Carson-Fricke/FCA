"""Continuous UTC-day planning for crypto pair backfill work.

Crypto trades and quotes are available every UTC calendar day.  This planner
therefore uses the crypto coverage calendar rather than the XNYS exchange
calendar and emits one queue item per pair and UTC day.  Catalog rows are the
resume checkpoint; complete and terminal-empty rows are not dispatched again,
while the current partial day remains replaceable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal, Protocol

from lake.catalog.ports import CatalogRepository
from lake.configuration import BackfillSettings, CryptoUniverse
from lake.contracts import AssetClass, DataType, symbol_filename
from lake.coverage import (
    CoverageSession,
    CoverageState,
    SessionCoverage,
    calculate_coverage,
)
from lake.storage.ports import QueueSender


class CryptoBackfillOrder(StrEnum):
    """Supported dispatch orderings for crypto UTC-day work."""

    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"


class _HasOrder(Protocol):
    order: str


OrderValue = Literal["newest_first", "oldest_first"]
_ACTIONABLE_STATES = frozenset(
    {CoverageState.MISSING, CoverageState.PARTIAL, CoverageState.ERROR}
)
_CRYPTO_PAIR = re.compile(r"[A-Z0-9]+/[A-Z0-9]+", re.ASCII)


@dataclass(frozen=True, slots=True)
class CryptoBackfillItem:
    """One pair-day request for the crypto ingest transaction."""

    symbol: str
    session: CoverageSession
    gaps: tuple[SessionCoverage, ...]
    query_end: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _canonical_pair(self.symbol))
        if any(gap.symbol != self.symbol for gap in self.gaps):
            raise ValueError("crypto gaps must share the requested pair")
        if any(gap.asset_class is not AssetClass.CRYPTO for gap in self.gaps):
            raise ValueError("crypto gaps must use the crypto asset class")
        if self.query_end is not None:
            object.__setattr__(self, "query_end", _utc_datetime(self.query_end))

    @property
    def pair(self) -> str:
        """Return the canonical crypto pair."""
        return self.symbol

    @property
    def session_date(self) -> date:
        """Return the UTC calendar date represented by this item."""
        return self.session.session_date

    @property
    def data_types(self) -> tuple[DataType, ...]:
        """Return requested gap types in stable trade-then-quote order."""
        present = {gap.data_type for gap in self.gaps}
        return tuple(data_type for data_type in _DATA_TYPES if data_type in present)

    @property
    def statuses(self) -> tuple[CoverageState, ...]:
        """Return distinct gap states in stable enum-value order."""
        return tuple(
            sorted({gap.state for gap in self.gaps}, key=lambda item: item.value)
        )

    @property
    def replace_existing(self) -> bool:
        """Whether this dispatch will replace an existing object or catalog row."""
        return any(gap.record is not None for gap in self.gaps)

    def queue_message(self) -> dict[str, object]:
        """Return the provider-independent message consumed by the queue handler."""
        end_time = self.query_end or self.session.close_time
        return {
            "asset_class": AssetClass.CRYPTO.value,
            "data_types": [data_type.value for data_type in self.data_types],
            "end_time": _format_utc(end_time),
            "replace_existing": self.replace_existing,
            "session_date": self.session_date.isoformat(),
            "start_time": _format_utc(self.session.open_time),
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class CryptoBackfillPlan:
    """A bounded, deterministic view of remaining crypto pair-day work."""

    start: date
    end: date
    order: CryptoBackfillOrder
    items: tuple[CryptoBackfillItem, ...]
    available_items: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", CryptoBackfillOrder(self.order))
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        if self.available_items < len(self.items):
            raise ValueError("available_items must include the selected items")

    def __iter__(self) -> Iterator[CryptoBackfillItem]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def has_more(self) -> bool:
        """Whether the work bound omitted remaining catalog gaps."""
        return len(self) < self.available_items

    @property
    def gaps(self) -> tuple[SessionCoverage, ...]:
        """Return all selected data-type gaps in item order."""
        return tuple(gap for item in self.items for gap in item.gaps)

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Return distinct selected UTC dates in dispatch order."""
        return tuple(dict.fromkeys(item.session_date for item in self.items))


def plan_crypto_backfill(
    catalog: CatalogRepository,
    *,
    pairs: Iterable[str] | str | None = None,
    data_types: Iterable[DataType | str] | DataType | str | None = None,
    universe: CryptoUniverse | None = None,
    backfill_settings: _HasOrder | BackfillSettings | None = None,
    order: CryptoBackfillOrder | OrderValue | str | None = None,
    as_of: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    start: date | None = None,
    end: date | None = None,
    max_items: int | None = None,
) -> CryptoBackfillPlan:
    """Plan all actionable crypto pair-days in an inclusive UTC date range.

    With no explicit range, the current UTC day is selected so a scheduler can
    refresh the open day.  A current day is intentionally actionable even when
    an earlier attempt recorded an empty result.  Future days are excluded
    because no provider read can safely cover data that has not occurred yet.
    """
    if max_items is not None and max_items < 1:
        raise ValueError("max_items must be positive")
    current_time = _resolve_clock(as_of, clock)
    selected_pairs, selected_data_types, configured_order = _configuration(
        pairs=pairs,
        data_types=data_types,
        universe=universe,
        backfill_settings=backfill_settings,
        order=order,
    )

    if start is None and end is None:
        start = end = current_time.date()
    elif start is None or end is None:
        raise ValueError("start and end must be supplied together")

    report = calculate_coverage(
        catalog,
        asset_class=AssetClass.CRYPTO,
        symbols=selected_pairs,
        data_types=selected_data_types,
        start=start,
        end=end,
        as_of=current_time,
    )
    grouped: dict[tuple[str, date], list[SessionCoverage]] = {}
    sessions: dict[tuple[str, date], CoverageSession] = {}
    for entry in report.entries:
        if entry.session.open_time > current_time:
            continue
        if entry.session.is_partial or entry.state in _ACTIONABLE_STATES:
            key = (entry.symbol, entry.session_date)
            grouped.setdefault(key, []).append(entry)
            sessions[key] = entry.session

    ordered = tuple(
        CryptoBackfillItem(
            symbol=symbol,
            session=sessions[(symbol, session_date)],
            gaps=tuple(
                sorted(
                    grouped[(symbol, session_date)],
                    key=lambda gap: _DATA_TYPE_ORDER[gap.data_type],
                )
            ),
            query_end=(
                current_time
                if sessions[(symbol, session_date)].is_partial
                else sessions[(symbol, session_date)].close_time
            ),
        )
        for symbol, session_date in sorted(
            grouped,
            key=lambda key: _item_sort_key(key, configured_order),
        )
    )
    selected = ordered if max_items is None else ordered[:max_items]
    return CryptoBackfillPlan(
        start,
        end,
        configured_order,
        selected,
        len(ordered),
    )


def enqueue_crypto_backfill(plan: CryptoBackfillPlan, sender: QueueSender) -> int:
    """Send planned crypto pair-days through an injected queue boundary."""
    for item in plan:
        sender.send(item.queue_message())
    return len(plan)


_DATA_TYPES = (DataType.TRADE, DataType.QUOTE)
_DATA_TYPE_ORDER = {data_type: index for index, data_type in enumerate(_DATA_TYPES)}


def _configuration(
    *,
    pairs: Iterable[str] | str | None,
    data_types: Iterable[DataType | str] | DataType | str | None,
    universe: CryptoUniverse | None,
    backfill_settings: _HasOrder | BackfillSettings | None,
    order: CryptoBackfillOrder | OrderValue | str | None,
) -> tuple[tuple[str, ...], tuple[DataType, ...], CryptoBackfillOrder]:
    if pairs is None and universe is not None:
        pairs = universe.pairs
    if data_types is None and universe is not None:
        data_types = universe.data_types
    if pairs is None:
        raise ValueError("pairs or universe must be supplied")
    if data_types is None:
        data_types = _DATA_TYPES

    selected_pairs = _normalize_pairs(pairs)
    selected_data_types = _normalize_data_types(data_types)
    configured_value: str | None = order
    if configured_value is None and backfill_settings is not None:
        configured_value = backfill_settings.order
    if configured_value is None:
        configured_value = CryptoBackfillOrder.NEWEST_FIRST.value
    try:
        configured_order = CryptoBackfillOrder(configured_value)
    except ValueError as error:
        raise ValueError("order must be 'newest_first' or 'oldest_first'") from error
    return selected_pairs, selected_data_types, configured_order


def _normalize_pairs(values: Iterable[str] | str) -> tuple[str, ...]:
    items = (values,) if isinstance(values, str) else values
    normalized = {_canonical_pair(value) for value in items}
    if not normalized:
        raise ValueError("pairs must not be empty")
    return tuple(sorted(normalized))


def _normalize_data_types(
    values: Iterable[DataType | str] | DataType | str,
) -> tuple[DataType, ...]:
    items = (values,) if isinstance(values, (DataType, str)) else values
    selected = {DataType(value) for value in items}
    if not selected or not selected.issubset(_DATA_TYPES):
        raise ValueError("crypto data_types must contain trades or quotes")
    return tuple(data_type for data_type in _DATA_TYPES if data_type in selected)


def _canonical_pair(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("crypto pairs must contain strings")
    pair = value.strip().upper()
    if _CRYPTO_PAIR.fullmatch(pair) is None:
        raise ValueError(f"invalid crypto pair: {value!r}")
    symbol_filename(pair)
    return pair


def _item_sort_key(
    key: tuple[str, date], order: CryptoBackfillOrder
) -> tuple[int, str]:
    symbol, session_date = key
    date_order = session_date.toordinal()
    if order is CryptoBackfillOrder.NEWEST_FIRST:
        date_order = -date_order
    return date_order, symbol


def _resolve_clock(
    as_of: datetime | None, clock: Callable[[], datetime] | None
) -> datetime:
    if as_of is not None and clock is not None:
        raise ValueError("as_of and clock must not both be supplied")
    value = (
        as_of
        if as_of is not None
        else (datetime.now(UTC) if clock is None else clock())
    )
    return _utc_datetime(value)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock and query bounds must be timezone-aware")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    normalized = _utc_datetime(value)
    return normalized.isoformat().replace("+00:00", "Z")


__all__ = [
    "CryptoBackfillItem",
    "CryptoBackfillOrder",
    "CryptoBackfillPlan",
    "enqueue_crypto_backfill",
    "plan_crypto_backfill",
]
