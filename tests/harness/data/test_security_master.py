"""Focused tests for the point-in-time security master."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

import harness.data.security_master as security_master_module
from harness.core.clock import Clock, SimClock
from harness.data.history import LookaheadError, to_nanos
from harness.data.security_master import (
    ListingsCatalogPort,
    SecurityMasterView,
    SecuritySnapshot,
)
from lake.catalog.memory import InMemoryListingRepository
from lake.catalog.models import DelistingType, ListingRecord, ListingType

RENAME_DAY = date(2022, 6, 9)


def episode(
    entity_id: str,
    symbol: str,
    *,
    listed: date,
    delisted: date | None = None,
    listing_type: ListingType = ListingType.IPO,
    delisting_type: DelistingType | None = None,
    final_price: str | None = None,
) -> ListingRecord:
    return ListingRecord(
        entity_id=entity_id,
        symbol=symbol,
        listed_date=listed,
        delisted_date=delisted,
        listing_type=listing_type,
        delisting_type=delisting_type,
        final_price=None if final_price is None else Decimal(final_price),
    )


FB = episode(
    "META",
    "FB",
    listed=date(2012, 5, 18),
    delisted=date(2022, 6, 8),
    listing_type=ListingType.IPO,
    delisting_type=DelistingType.RENAME,
    final_price="196.35",
)
META = episode("META", "META", listed=RENAME_DAY, listing_type=ListingType.RENAME)
TSLA = episode("TSLA", "TSLA", listed=date(2010, 6, 29))
TWTR = episode(
    "TWTR",
    "TWTR",
    listed=date(2013, 11, 7),
    delisted=date(2022, 10, 28),
    delisting_type=DelistingType.ACQUISITION,
    final_price="53.70",
)
PENDING = episode(
    "EARLY",
    "EARLY",
    listed=date(2020, 2, 20),
    delisted=date(2026, 12, 31),
    delisting_type=DelistingType.VOLUNTARY,
    final_price="10.00",
)
FUTURE = episode("FUTR", "FUTR", listed=date(2026, 1, 2))

ALL_EPISODES = (FB, META, TSLA, TWTR, PENDING, FUTURE)


def view(
    head: datetime | None = None,
    episodes: tuple[ListingRecord, ...] = ALL_EPISODES,
) -> SecurityMasterView:
    clock = SimClock(head or datetime(2024, 6, 28, 20, tzinfo=UTC))
    return SecurityMasterView(clock, InMemoryListingRepository(episodes))


def instant(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 20, tzinfo=UTC)


def test_resolve_proves_point_in_time_visibility_for_the_active_symbol() -> None:
    snapshot = view().resolve("META")

    assert snapshot is not None
    assert snapshot.entity_id == "META"
    assert snapshot.queried_symbol == "META"
    assert snapshot.symbol == "META"
    assert snapshot.is_listed
    assert snapshot.listed_date == date(2012, 5, 18)
    assert snapshot.delisted_date is None
    assert snapshot.symbols == ("FB", "META")


def test_rename_fixture_resolves_historical_symbols_through_one_entity() -> None:
    master = view()

    from_old = master.resolve("FB")
    from_new = master.resolve("META")

    assert from_old is not None and from_new is not None
    assert from_old.entity_id == from_new.entity_id == "META"
    assert from_old.symbols == ("FB", "META") == from_new.symbols
    assert from_old.is_listed and from_new.is_listed
    assert len({from_old.entity_id, from_new.entity_id}) == 1


def test_as_of_before_rename_never_exposes_the_rename() -> None:
    master = view()
    as_of = datetime(2020, 1, 2, 21, tzinfo=UTC)

    snapshot = master.resolve("FB", as_of)

    assert snapshot is not None
    assert snapshot.entity_id == "META"
    assert snapshot.symbols == ("FB",)
    assert snapshot.symbol == "FB"
    assert snapshot.delisted_date is None
    assert master.resolve("META", as_of) is None
    assert master.entity_id("META", as_of) is None
    assert not master.is_listed("META", as_of)


def test_future_delisting_stays_unknown_and_listing_stays_active() -> None:
    snapshot = view().resolve("EARLY")

    assert snapshot is not None
    assert snapshot.is_listed
    assert snapshot.delisted_date is None
    assert snapshot.delisting_type is None
    assert snapshot.final_price is None


def test_proven_delisting_exposes_dates_types_and_final_price() -> None:
    snapshot = view().resolve("TWTR")

    assert snapshot is not None
    assert not snapshot.is_listed
    assert snapshot.delisted_date == date(2022, 10, 28)
    assert snapshot.delisting_type is DelistingType.ACQUISITION
    assert snapshot.final_price == Decimal("53.70")
    assert snapshot.symbol == "TWTR"


def test_delisting_becomes_visible_only_when_the_clock_reaches_it() -> None:
    before = view(head=datetime(2026, 12, 30, 21, tzinfo=UTC)).resolve("EARLY")
    after = view(head=datetime(2027, 1, 4, 21, tzinfo=UTC)).resolve("EARLY")

    assert before is not None and after is not None
    assert before.is_listed and before.delisted_date is None
    assert not after.is_listed
    assert after.delisted_date == date(2026, 12, 31)
    assert after.delisting_type is DelistingType.VOLUNTARY
    assert after.final_price == Decimal("10.00")


def test_future_listings_are_invisible_to_every_query() -> None:
    master = view()

    assert master.resolve("FUTR") is None
    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        master.resolve("FUTR", datetime(2026, 7, 1, 21, tzinfo=UTC))
    assert "FUTR" not in master.listed_symbols()


def test_resolve_newer_than_clock_head_raises_lookahead_error() -> None:
    master = view(head=datetime(2024, 6, 28, 20, tzinfo=UTC))

    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        master.resolve("META", datetime(2024, 6, 28, 20, 0, 1, tzinfo=UTC))
    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        master.listed_symbols(to_nanos(datetime(2025, 1, 1, tzinfo=UTC)))


def test_resolve_accepts_clock_head_nanoseconds_without_error() -> None:
    master = view(head=datetime(2024, 6, 28, 20, tzinfo=UTC))

    snapshot = master.resolve("META", to_nanos(datetime(2024, 6, 28, 20, tzinfo=UTC)))

    assert snapshot is not None
    assert snapshot.as_of == date(2024, 6, 28)


def test_active_symbol_flips_exactly_at_the_rename_boundary() -> None:
    master = view()

    assert master.active_symbol("META", datetime(2022, 6, 8, 21, tzinfo=UTC)) == "FB"
    assert master.active_symbol("META", datetime(2022, 6, 9, 21, tzinfo=UTC)) == "META"
    assert master.active_symbol("META", datetime(2020, 1, 2, 21, tzinfo=UTC)) == "FB"
    assert master.active_symbol("TWTR") is None
    assert master.active_symbol("UNKNOWN") is None


def test_listed_symbols_return_one_symbol_per_entity_in_deterministic_order() -> None:
    master = view()

    assert master.listed_symbols() == ("EARLY", "META", "TSLA")
    assert master.listed_symbols(datetime(2020, 1, 2, 21, tzinfo=UTC)) == (
        "FB",
        "TSLA",
        "TWTR",
    )
    assert len(master.listed_symbols()) == len(set(master.listed_symbols()))


def test_history_only_contains_episodes_known_on_the_as_of_date() -> None:
    master = view()

    assert [item.symbol for item in master.history("META")] == ["FB", "META"]
    assert [
        item.symbol
        for item in master.history("META", datetime(2020, 1, 2, 21, tzinfo=UTC))
    ] == ["FB"]
    assert master.history("UNKNOWN") == ()


def test_unknown_symbols_resolve_to_none_without_raising() -> None:
    master = view()

    assert master.resolve("ZZZZ") is None
    assert master.entity_id("ZZZZ") is None
    assert not master.is_listed("ZZZZ")


def test_snapshot_construction_is_the_structural_clipping_chokepoint() -> None:
    snapshot = SecuritySnapshot(
        entity_id="meta",
        as_of=date(2020, 1, 2),
        queried_symbol=" fb ",
        episodes=(META, FB),
    )

    assert snapshot.entity_id == "META"
    assert snapshot.queried_symbol == "FB"
    assert snapshot.episodes == (FB,)
    assert snapshot.symbols == ("FB",)
    assert snapshot.symbol == "FB"
    assert snapshot.is_listed
    assert snapshot.delisted_date is None


def test_snapshot_rejects_empty_known_chains_and_future_days() -> None:
    with pytest.raises(ValueError, match="at least one known episode"):
        SecuritySnapshot(
            entity_id="META",
            as_of=date(2010, 1, 2),
            queried_symbol="FB",
            episodes=(FB, META),
        )
    snapshot = view().resolve("META")
    assert snapshot is not None
    with pytest.raises(ValueError, match="on or before the snapshot as_of"):
        snapshot.episode_on(date(2025, 1, 2))


def test_episode_on_walks_the_rename_chain_without_overlap() -> None:
    snapshot = view().resolve("FB")
    assert snapshot is not None

    assert snapshot.episode_on(date(2020, 6, 8)) is FB
    assert snapshot.episode_on(date(2022, 6, 8)) is FB
    assert snapshot.episode_on(RENAME_DAY) is META
    assert snapshot.episode_on(date(2024, 6, 28)) is META


def test_repeated_resolution_is_deterministic() -> None:
    master = view()

    assert master.resolve("FB") == master.resolve("FB")
    assert master.resolve("META") == view().resolve("META")
    assert master.listed_symbols() == view().listed_symbols()


def test_view_requires_clock_and_listings_ports() -> None:
    with pytest.raises(TypeError, match="clock must implement Clock"):
        SecurityMasterView(cast(Clock, "not-a-clock"), InMemoryListingRepository())
    with pytest.raises(TypeError, match="listings must implement"):
        SecurityMasterView(
            SimClock(datetime(2024, 1, 2, tzinfo=UTC)),
            cast(ListingsCatalogPort, "not-a-repository"),
        )


def test_listings_port_is_satisfied_by_the_memory_repository() -> None:
    assert isinstance(InMemoryListingRepository(), ListingsCatalogPort)


def test_security_master_module_avoids_wall_clock_randomness_and_network() -> None:
    source = inspect.getsource(security_master_module)

    assert "datetime.now" not in source
    assert "import random" not in source
    assert "time.time" not in source
    assert "urlopen" not in source
    assert "requests.get" not in source
    assert "httpx" not in source
