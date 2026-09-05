"""Focused tests for listing lifecycle event derivation and settlement."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

import harness.data.listing_events as listing_events_module
from harness.core.clock import Clock, SimClock
from harness.data.history import LookaheadError, to_nanos
from harness.data.listing_events import (
    CorporateActionKind,
    DelistingEvent,
    ListingEventDeriver,
    PessimisticDelistingSettlement,
    RenameEvent,
    UnknownDelistingPriceError,
)
from harness.data.security_master import ListingsCatalogPort
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
BANKRUPT = episode(
    "FAIL",
    "FAIL",
    listed=date(2019, 1, 2),
    delisted=RENAME_DAY,
    delisting_type=DelistingType.BANKRUPTCY,
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
ORPHAN_RENAME = episode(
    "ORPH", "ORPH2", listed=date(2020, 3, 2), listing_type=ListingType.RENAME
)

ALL_EPISODES = (FB, META, TSLA, TWTR, BANKRUPT, PENDING, FUTURE, ORPHAN_RENAME)


def deriver(
    head: datetime | None = None,
    episodes: tuple[ListingRecord, ...] = ALL_EPISODES,
) -> ListingEventDeriver:
    clock = SimClock(head or datetime(2024, 6, 28, 20, tzinfo=UTC))
    return ListingEventDeriver(clock, InMemoryListingRepository(episodes))


def instant(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 20, tzinfo=UTC)


def test_rename_surfaces_exactly_on_the_successor_first_session() -> None:
    before = deriver().pending(instant(2022, 6, 8))
    on_day = deriver().pending(instant(2022, 6, 9))

    assert all(not isinstance(event, RenameEvent) for event in before)
    assert (
        RenameEvent(
            entity_id="META", known_date=RENAME_DAY, old_symbol="FB", new_symbol="META"
        )
        in on_day
    )


def test_rename_preserves_one_entity_identity_across_symbols() -> None:
    events = deriver().poll(instant(2022, 6, 9))
    renames = [event for event in events if isinstance(event, RenameEvent)]

    assert len(renames) == 1
    rename = renames[0]
    assert rename.entity_id == "META"
    assert rename.old_symbol == "FB"
    assert rename.new_symbol == "META"
    assert rename.kind is CorporateActionKind.RENAME


def test_rename_delisting_episodes_produce_renames_not_delistings() -> None:
    events = deriver().poll(instant(2022, 6, 9))
    meta_symbols = {event.symbol_key for event in events if event.entity_id == "META"}

    assert meta_symbols == {"FB"}
    assert not any(
        isinstance(event, DelistingEvent) and event.entity_id == "META"
        for event in events
    )


def test_delisting_surfaces_on_the_delisting_date_with_type_and_price() -> None:
    before = deriver().pending(instant(2022, 10, 27))
    on_day = deriver().poll(instant(2022, 10, 28))

    assert all(
        not (isinstance(event, DelistingEvent) and event.symbol == "TWTR")
        for event in before
    )
    assert (
        DelistingEvent(
            entity_id="TWTR",
            known_date=date(2022, 10, 28),
            symbol="TWTR",
            delisting_type=DelistingType.ACQUISITION,
            final_price=Decimal("53.70"),
        )
        in on_day
    )


def test_delisting_without_final_price_keeps_it_none() -> None:
    events = deriver().poll(instant(2022, 6, 9))
    delisting = next(
        event
        for event in events
        if isinstance(event, DelistingEvent) and event.entity_id == "FAIL"
    )

    assert delisting.delisting_type is DelistingType.BANKRUPTCY
    assert delisting.final_price is None


def test_future_delistings_stay_unknown() -> None:
    events = deriver().poll()

    assert not any(event.entity_id == "EARLY" for event in events)
    assert not any(event.entity_id == "FUTR" for event in events)


def test_rename_without_a_visible_predecessor_surfaces_nothing() -> None:
    events = deriver().poll(instant(2020, 3, 2))

    assert not any(event.entity_id == "ORPH" for event in events)


def test_poll_is_idempotent_and_delivered_accumulates_in_order() -> None:
    events = deriver()
    first = events.poll(instant(2022, 6, 9))
    replay = events.poll(instant(2022, 6, 9))
    later = events.poll(instant(2022, 10, 28))

    assert first
    assert replay == ()
    assert events.poll() == ()
    assert later
    assert events.delivered == (*first, *later)
    assert [event.known_date for event in events.delivered] == sorted(
        event.known_date for event in events.delivered
    )


def test_pending_does_not_consume_due_events() -> None:
    events = deriver()

    first = events.pending(instant(2022, 6, 9))
    second = events.pending(instant(2022, 6, 9))

    assert first
    assert first == second
    assert events.delivered == ()


def test_delivery_order_is_deterministic_across_entities_and_kinds() -> None:
    same_day = (
        episode(
            "AAA",
            "AAA",
            listed=date(2000, 1, 3),
            delisted=date(2022, 6, 9),
            delisting_type=DelistingType.MERGER,
            final_price="7.00",
        ),
        FB,
        META,
        BANKRUPT,
    )
    events = deriver(episodes=same_day).poll(instant(2022, 6, 9))

    assert [event.sort_key() for event in events] == sorted(
        event.sort_key() for event in events
    )
    assert events == (
        DelistingEvent(
            entity_id="AAA",
            known_date=date(2022, 6, 9),
            symbol="AAA",
            delisting_type=DelistingType.MERGER,
            final_price=Decimal("7.00"),
        ),
        DelistingEvent(
            entity_id="FAIL",
            known_date=date(2022, 6, 9),
            symbol="FAIL",
            delisting_type=DelistingType.BANKRUPTCY,
        ),
        RenameEvent(
            entity_id="META",
            known_date=RENAME_DAY,
            old_symbol="FB",
            new_symbol="META",
        ),
    )


def test_explicit_as_of_newer_than_clock_head_raises_lookahead_error() -> None:
    events = deriver(head=instant(2024, 6, 28))

    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        events.poll(datetime(2024, 6, 28, 20, 0, 1, tzinfo=UTC))
    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        events.pending(to_nanos(datetime(2025, 1, 1, tzinfo=UTC)))


def test_explicit_as_of_at_the_clock_head_is_accepted() -> None:
    events = deriver(head=instant(2022, 10, 28))

    polled = events.poll(to_nanos(datetime(2022, 10, 28, 20, tzinfo=UTC)))

    assert any(
        isinstance(event, DelistingEvent) and event.entity_id == "TWTR"
        for event in polled
    )


def test_deriver_requires_clock_and_listings_ports() -> None:
    with pytest.raises(TypeError, match="clock must implement Clock"):
        ListingEventDeriver(cast(Clock, "not-a-clock"), InMemoryListingRepository())
    with pytest.raises(TypeError, match="listings must implement"):
        ListingEventDeriver(
            SimClock(instant(2024, 1, 2)), cast(ListingsCatalogPort, "not-a-repo")
        )


def test_listings_port_is_satisfied_by_the_memory_repository() -> None:
    assert isinstance(InMemoryListingRepository(), ListingsCatalogPort)


def test_events_are_canonical_frozen_and_deduplicatable() -> None:
    delisting = DelistingEvent(
        entity_id="meta",
        known_date=date(2022, 10, 28),
        symbol=" twtr ",
        delisting_type=DelistingType.ACQUISITION,
        final_price=cast(Decimal, "53.70"),
    )
    clone = DelistingEvent(
        entity_id="META",
        known_date=date(2022, 10, 28),
        symbol="TWTR",
        delisting_type=DelistingType.ACQUISITION,
        final_price=Decimal("53.70"),
    )

    assert delisting == clone
    assert hash(delisting) == hash(clone)
    assert delisting.symbol == "TWTR"
    assert delisting.final_price == Decimal("53.70")
    with pytest.raises(ValueError, match="a rename must change the symbol"):
        RenameEvent(
            entity_id="META",
            known_date=RENAME_DAY,
            old_symbol="FB",
            new_symbol="fb",
        )
    with pytest.raises(TypeError, match="known_date must be a date"):
        DelistingEvent(
            entity_id="META",
            known_date=datetime(2022, 10, 28, tzinfo=UTC),
            symbol="TWTR",
            delisting_type=DelistingType.ACQUISITION,
        )


def test_pessimistic_settlement_settles_known_prices_exactly() -> None:
    policy = PessimisticDelistingSettlement()

    assert policy.settlement_value(Decimal("100"), Decimal("53.70")) == Decimal(
        "5370.00"
    )
    assert policy.settlement_value(Decimal("-40"), Decimal("2.50")) == Decimal(
        "-100.00"
    )


def test_pessimistic_settlement_sets_unknown_longs_to_worthless() -> None:
    policy = PessimisticDelistingSettlement()

    assert policy.settlement_value(Decimal("100"), None) == Decimal("0")
    assert policy.settlement_value(Decimal("0"), None) == Decimal("0")
    with pytest.raises(UnknownDelistingPriceError, match="requires"):
        policy.settlement_value(Decimal("-100"), None)


def test_module_avoids_wall_clock_randomness_and_network() -> None:
    source = inspect.getsource(listing_events_module)

    assert "datetime.now" not in source
    assert "import random" not in source
    assert "time.time" not in source
    assert "urlopen" not in source
    assert "requests.get" not in source
    assert "httpx" not in source
