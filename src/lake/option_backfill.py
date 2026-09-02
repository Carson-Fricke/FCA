"""Deterministic, resumable planning for option front-set backfills."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

from lake.catalog import (
    CatalogRepository,
    ContractRecord,
    CoverageRecord,
    CoverageStatus,
)
from lake.contracts import ContractsCatalog, DailyFrontSet
from lake.coverage import CoverageSession, equity_sessions
from lake.option_selector import OptionChainSelector
from lake.partition_paths import AssetClass, DataType
from lake.storage import QueueSender
from lake.universes import OptionsUniverse


class OptionSpotProvider(Protocol):
    """Resolve the underlying spot used for one daily front-set selection."""

    def __call__(self, underlying: str, selection_date: date) -> Spot:
        """Return an exact or provider-normalized positive spot value."""


Spot = Decimal | int | float | str
SpotValues = Mapping[str, Spot] | OptionSpotProvider
OptionClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class OptionBackfillItem:
    """One selected OSI symbol-day request for the shared queue worker."""

    contract: ContractRecord
    session: CoverageSession
    data_types: tuple[DataType, ...]
    replace_existing: bool = False

    @property
    def symbol(self) -> str:
        """Return the selected OSI symbol."""
        return self.contract.symbol

    @property
    def session_date(self) -> date:
        """Return the XNYS session date."""
        return self.session.session_date

    def queue_message(self) -> dict[str, object]:
        """Return the complete provider-independent worker payload."""
        return {
            "asset_class": AssetClass.OPTION.value,
            "data_types": [data_type.value for data_type in self.data_types],
            "end_time": _format_datetime(self.session.close_time),
            "expiration_date": self.contract.expiration_date.isoformat(),
            "option_type": self.contract.option_type.value,
            "replace_existing": self.replace_existing,
            "session_date": self.session_date.isoformat(),
            "start_time": _format_datetime(self.session.open_time),
            "strike_price": str(self.contract.strike_price),
            "symbol": self.contract.symbol,
            "underlying": self.contract.underlying,
        }


@dataclass(frozen=True, slots=True)
class OptionBackfillPlan:
    """Daily front-set work in deterministic session, underlying, OSI order."""

    start: date
    end: date
    items: tuple[OptionBackfillItem, ...]
    front_sets: tuple[DailyFrontSet, ...]
    available_items: int

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        if self.available_items < len(self.items):
            raise ValueError("available_items must include the selected items")

    def __iter__(self) -> Iterator[OptionBackfillItem]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def has_more(self) -> bool:
        """Whether a work limit omitted remaining actionable OSI symbol-days."""
        return len(self) < self.available_items

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Return distinct session dates in dispatch order."""
        return tuple(dict.fromkeys(item.session_date for item in self.items))


def plan_option_backfill(
    catalog: ContractsCatalog,
    coverage: CatalogRepository,
    selector: OptionChainSelector,
    *,
    underlyings: Iterable[str] | str | None = None,
    universe: OptionsUniverse | None = None,
    spots: SpotValues,
    data_types: Iterable[DataType | str] | DataType | str | None = None,
    start: date,
    end: date,
    as_of: datetime | None = None,
    clock: OptionClock | None = None,
    max_items: int | None = None,
) -> OptionBackfillPlan:
    """Persist daily front sets and plan only non-terminal option coverage.

    The coverage catalog is the resume checkpoint.  A later call selects and
    replaces the same front-set key, then omits OSI symbol-days whose requested
    trade and quote rows are already ``complete`` or terminal ``empty``.  Spot
    values are caller-supplied so planning remains deterministic and offline.
    """
    if max_items is not None and max_items < 1:
        raise ValueError("max_items must be positive")
    selected_underlyings = _configuration_underlyings(underlyings, universe)
    selected_data_types = _normalize_data_types(
        data_types if data_types is not None else (DataType.TRADE, DataType.QUOTE)
    )
    current_time = _resolve_clock(as_of, clock)
    sessions = tuple(
        session
        for session in equity_sessions(start, end, as_of=current_time)
        if session.close_time <= current_time
    )
    coverage_by_key = {
        (record.symbol, record.session_date, record.data_type): record
        for record in coverage.list(
            asset_class=AssetClass.OPTION,
            start=start,
            end=end,
        )
    }

    front_sets: list[DailyFrontSet] = []
    items: list[OptionBackfillItem] = []
    for session in sessions:
        for underlying in selected_underlyings:
            spot = _spot(spots, underlying, session.session_date)
            contracts = selector.select_from_repository(
                catalog.contracts,
                underlying=underlying,
                selection_date=session.session_date,
                spot=spot,
            )
            front_set = DailyFrontSet(
                underlying=underlying,
                selection_date=session.session_date,
                contracts=contracts,
            )
            if universe is None or universe.selection.persist_front_set:
                catalog.upsert_front_set(front_set)
            front_sets.append(front_set)
            for contract in contracts:
                if _is_terminal(
                    coverage_by_key,
                    contract.symbol,
                    session.session_date,
                    selected_data_types,
                ):
                    continue
                items.append(
                    OptionBackfillItem(
                        contract,
                        session,
                        selected_data_types,
                        _replace_existing(
                            coverage_by_key,
                            contract.symbol,
                            session.session_date,
                            selected_data_types,
                        ),
                    )
                )

    ordered = tuple(items)
    selected = ordered if max_items is None else ordered[:max_items]
    return OptionBackfillPlan(
        start=start,
        end=end,
        items=selected,
        front_sets=tuple(front_sets),
        available_items=len(ordered),
    )


def enqueue_option_backfill(plan: OptionBackfillPlan, sender: QueueSender) -> int:
    """Dispatch option work through the existing queue sender boundary."""
    for item in plan:
        sender.send(item.queue_message())
    return len(plan)


def _configuration_underlyings(
    underlyings: Iterable[str] | str | None,
    universe: OptionsUniverse | None,
) -> tuple[str, ...]:
    values: Iterable[str] | str
    if underlyings is None:
        if universe is None:
            raise ValueError("underlyings or universe must be supplied")
        values = universe.soak.underlyings
    else:
        values = underlyings
    candidates = (values,) if isinstance(values, str) else values
    normalized: set[str] = set()
    for value in candidates:
        if not isinstance(value, str):
            raise TypeError("underlyings entries must be strings")
        normalized.add(value.strip().upper())
    if not normalized or "" in normalized:
        raise ValueError("underlyings must contain non-empty strings")
    return tuple(sorted(normalized))


def _normalize_data_types(
    values: Iterable[DataType | str] | DataType | str,
) -> tuple[DataType, ...]:
    candidates = (values,) if isinstance(values, (DataType, str)) else values
    normalized = {DataType(value) for value in candidates}
    if not normalized:
        raise ValueError("data_types must not be empty")
    unsupported = normalized - {DataType.TRADE, DataType.QUOTE}
    if unsupported:
        raise ValueError("option data_types must be trades or quotes")
    return tuple(
        data_type
        for data_type in (DataType.TRADE, DataType.QUOTE)
        if data_type in normalized
    )


def _spot(spots: SpotValues, underlying: str, session_date: date) -> Spot:
    if isinstance(spots, Mapping):
        try:
            return spots[underlying]
        except KeyError as error:
            raise ValueError(f"spot is missing for underlying {underlying}") from error
    return spots(underlying, session_date)


def _is_terminal(
    coverage: Mapping[tuple[str, date, DataType], CoverageRecord],
    symbol: str,
    session_date: date,
    data_types: Iterable[DataType],
) -> bool:
    return all(
        coverage.get((symbol, session_date, data_type)) is not None
        and coverage[(symbol, session_date, data_type)].status
        in {CoverageStatus.COMPLETE, CoverageStatus.EMPTY}
        for data_type in data_types
    )


def _replace_existing(
    coverage: Mapping[tuple[str, date, DataType], CoverageRecord],
    symbol: str,
    session_date: date,
    data_types: Iterable[DataType],
) -> bool:
    return any(
        coverage.get((symbol, session_date, data_type), None) is not None
        and coverage[(symbol, session_date, data_type)].status is CoverageStatus.PARTIAL
        for data_type in data_types
    )


def _normalize_as_of(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _resolve_clock(
    as_of: datetime | None,
    clock: OptionClock | None,
) -> datetime:
    if as_of is not None and clock is not None:
        raise ValueError("as_of and clock must not both be supplied")
    return _normalize_as_of(as_of if as_of is not None else clock() if clock else None)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "OptionBackfillItem",
    "OptionBackfillPlan",
    "OptionClock",
    "OptionSpotProvider",
    "enqueue_option_backfill",
    "plan_option_backfill",
]
