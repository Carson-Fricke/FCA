"""Newest-first, resumable equity backfill planning.

This module adds backfill priority and work bounding on top of
:mod:`lake.equity_catchup`.  Coverage classification and queue message shapes
remain owned by that module.  A rerun is resumable because the catalog is read
again for every plan; complete and terminal-empty rows therefore disappear
without a process-local cursor.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Protocol

from lake.catalog import CatalogRepository
from lake.equity_catchup import (
    CatchUpOrder,
    EquityCatchUpItem,
    EquityCatchUpPlan,
    enqueue_equity_catch_up,
    plan_equity_catch_up,
)
from lake.history_valve import HistoryValve
from lake.partition_paths import DataType, symbol_filename
from lake.settings import BackfillSettings
from lake.storage import QueueSender
from lake.storage_cap import StorageCapGuard
from lake.universes import EquityUniverse


class _BackfillSettings(Protocol):
    order: str
    liquidity_rank_tiebreaker: bool


@dataclass(frozen=True, slots=True)
class EquityBackfillPlan:
    """A bounded, deterministic view of remaining equity catch-up work."""

    catch_up: EquityCatchUpPlan
    available_items: int

    def __post_init__(self) -> None:
        if self.available_items < len(self.catch_up):
            raise ValueError("available_items must include the selected items")
        if self.catch_up.order is not CatchUpOrder.NEWEST_FIRST:
            raise ValueError("equity backfill order must be newest_first")

    def __iter__(self) -> Iterator[EquityCatchUpItem]:
        return iter(self.catch_up)

    def __len__(self) -> int:
        return len(self.catch_up)

    @property
    def items(self) -> tuple[EquityCatchUpItem, ...]:
        """Return the selected work items in dispatch order."""
        return self.catch_up.items

    @property
    def start(self) -> date:
        """Return the inclusive start of the planned session range."""
        return self.catch_up.start

    @property
    def end(self) -> date:
        """Return the inclusive end of the planned session range."""
        return self.catch_up.end

    @property
    def has_more(self) -> bool:
        """Whether the work bound omitted remaining catalog gaps."""
        return len(self) < self.available_items

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Return distinct selected session dates in dispatch order."""
        return self.catch_up.session_dates


def plan_equity_backfill(
    catalog: CatalogRepository,
    *,
    symbols: Iterable[str] | str | None = None,
    data_types: Iterable[DataType | str] | DataType | str | None = None,
    universe: EquityUniverse | None = None,
    backfill_settings: _BackfillSettings | BackfillSettings | None = None,
    liquidity_ranks: Mapping[str, int] | None = None,
    history_valve: HistoryValve | None = None,
    max_items: int | None = None,
    as_of: datetime | None = None,
    start: date | None = None,
    end: date | None = None,
) -> EquityBackfillPlan:
    """Plan remaining equity gaps newest-first with an optional work bound.

    Session date is the primary key.  Within a session, lower liquidity rank
    wins, followed by canonical symbol and the existing data-type ordering in
    each catch-up item.  Missing ranks sort after ranked symbols and are then
    ordered by symbol.  ``liquidity_ranks`` is caller-supplied metadata, so the
    planner has no provider or Azure dependency.

    The catalog is the resume checkpoint: after a successful queue delivery
    records complete or terminal-empty coverage, calling this function again
    excludes that work.  ``max_items`` bounds the returned dispatch batch; use
    ``has_more`` to determine whether another bounded plan is available.
    """
    if max_items is not None and max_items < 1:
        raise ValueError("max_items must be positive")

    catch_up = plan_equity_catch_up(
        catalog,
        symbols=symbols,
        data_types=data_types,
        universe=universe,
        backfill_settings=backfill_settings,
        order=CatchUpOrder.NEWEST_FIRST,
        as_of=as_of,
        start=start,
        end=end,
    )
    ranks = _normalize_liquidity_ranks(liquidity_ranks)
    use_ranks = _liquidity_rank_tiebreaker(backfill_settings)
    valve = history_valve
    if valve is None and universe is not None:
        valve = HistoryValve.from_universe(universe)
    items = _filter_by_history_valve(catch_up.items, valve, ranks)
    ordered = tuple(
        sorted(
            items,
            key=lambda item: _backfill_sort_key(item, ranks, use_ranks),
        )
    )
    selected = ordered if max_items is None else ordered[:max_items]
    return EquityBackfillPlan(
        catch_up=replace(catch_up, items=selected),
        available_items=len(ordered),
    )


def enqueue_equity_backfill(
    plan: EquityBackfillPlan,
    sender: QueueSender,
    *,
    storage_guard: StorageCapGuard | None = None,
) -> int:
    """Dispatch a plan, stopping explicitly when enqueue is at capacity."""
    if storage_guard is None:
        return enqueue_equity_catch_up(plan.catch_up, sender)
    for item in plan:
        storage_guard.enforce_enqueue()
        sender.send(item.queue_message())
    return len(plan)


def _normalize_liquidity_ranks(
    values: Mapping[str, int] | None,
) -> dict[str, int]:
    if values is None:
        return {}
    result: dict[str, int] = {}
    for symbol, rank in values.items():
        if not isinstance(symbol, str):
            raise TypeError("liquidity rank symbols must be strings")
        canonical = symbol.strip().upper()
        symbol_filename(canonical)
        if canonical in result:
            raise ValueError(f"duplicate liquidity rank for symbol: {canonical}")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError("liquidity ranks must be positive integers")
        result[canonical] = rank
    return result


def _liquidity_rank_tiebreaker(
    settings: _BackfillSettings | BackfillSettings | None,
) -> bool:
    return True if settings is None else settings.liquidity_rank_tiebreaker


def _filter_by_history_valve(
    items: Iterable[EquityCatchUpItem],
    valve: HistoryValve | None,
    ranks: Mapping[str, int],
) -> tuple[EquityCatchUpItem, ...]:
    if valve is None:
        return tuple(items)

    filtered: list[EquityCatchUpItem] = []
    for item in items:
        rank = ranks.get(item.symbol)
        gaps = tuple(
            gap for gap in item.gaps if valve.allows(gap.data_type, liquidity_rank=rank)
        )
        if gaps:
            filtered.append(replace(item, gaps=gaps))
    return tuple(filtered)


def _backfill_sort_key(
    item: EquityCatchUpItem,
    ranks: Mapping[str, int],
    use_ranks: bool,
) -> tuple[int, int, int, str]:
    rank = ranks.get(item.symbol)
    return (
        -item.session_date.toordinal(),
        0 if use_ranks and rank is not None else 1,
        rank if use_ranks and rank is not None else 0,
        item.symbol,
    )


__all__ = [
    "EquityBackfillPlan",
    "enqueue_equity_backfill",
    "plan_equity_backfill",
]
