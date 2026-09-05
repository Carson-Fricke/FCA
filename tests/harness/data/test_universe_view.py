"""Focused tests for the point-in-time universe view.

The dynamic-subscription tests at the bottom pin the deterministic engine
boundary contract documented in :mod:`harness.data.universe_view` using
fakes for the strategy subscription set, so the data-layer tests never
import the concurrently-owned ``harness.strategy`` modules.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

import harness.data.universe_view as universe_view_module
from harness.core.clock import Clock, SimClock
from harness.data.history import LookaheadError, to_nanos
from harness.data.universe_view import (
    EquityListingUniverse,
    MemberKind,
    OptionContractUniverse,
    UniverseComposition,
    UniverseMember,
    UniverseSnapshot,
    UniverseSource,
    UniverseView,
)
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryDailyFrontSetRepository,
)
from lake.catalog.memory import InMemoryContractRepository, InMemoryListingRepository
from lake.catalog.models import (
    ContractRecord,
    DelistingType,
    ListingRecord,
    ListingType,
    OptionType,
)

HEAD = datetime(2024, 6, 28, 20, tzinfo=UTC)

JAN = date(2024, 1, 19)
FEB = date(2024, 2, 16)


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


def contract(
    symbol: str,
    *,
    expiry: date = JAN,
    strike: str = "150",
    right: OptionType = OptionType.CALL,
    listed: date | None = None,
    delisted: date | None = None,
) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying="AAPL",
        expiration_date=expiry,
        strike_price=Decimal(strike),
        option_type=right,
        listed_date=listed,
        delisted_date=delisted,
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
META = episode("META", "META", listed=date(2022, 6, 9), listing_type=ListingType.RENAME)
TSLA = episode("TSLA", "TSLA", listed=date(2010, 6, 29))
TWTR = episode(
    "TWTR",
    "TWTR",
    listed=date(2013, 11, 7),
    delisted=date(2022, 10, 28),
    delisting_type=DelistingType.ACQUISITION,
    final_price="53.70",
)
EARLY = episode(
    "EARLY",
    "EARLY",
    listed=date(2020, 2, 20),
    delisted=date(2026, 12, 31),
    delisting_type=DelistingType.VOLUNTARY,
    final_price="10.00",
)
FUTURE = episode("FUTR", "FUTR", listed=date(2026, 1, 2))

ALL_EPISODES = (FB, META, TSLA, TWTR, EARLY, FUTURE)

JAN_C150 = contract("AAPL240119C00150000")
JAN_C155 = contract("AAPL240119C00155000", strike="155")
JAN_P150 = contract("AAPL240119P00150000", right=OptionType.PUT)
FEB_C150 = contract("AAPL240216C00150000", expiry=FEB)
LATE_LISTED = contract("AAPL240119C00160000", strike="160", listed=date(2024, 1, 17))
DELISTED = contract("AAPL240119C00161000", strike="161", delisted=date(2024, 1, 10))

WEEK_MEMBERSHIP = (JAN_C150, JAN_C155, JAN_P150)


def instant(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 20, tzinfo=UTC)


def listing_repo(episodes: tuple[ListingRecord, ...]) -> InMemoryListingRepository:
    return InMemoryListingRepository(episodes)


def contract_catalog(
    contracts: tuple[ContractRecord, ...],
    front_sets: tuple[DailyFrontSet, ...] = (),
) -> ContractsCatalog:
    return ContractsCatalog(
        InMemoryContractRepository(contracts),
        InMemoryDailyFrontSetRepository(front_sets),
    )


def front_set(selection_date: date, *contracts: ContractRecord) -> DailyFrontSet:
    return DailyFrontSet(
        underlying="AAPL", selection_date=selection_date, contracts=contracts
    )


def week_front_sets() -> tuple[DailyFrontSet, ...]:
    week = tuple(
        front_set(date(2024, 1, number), *WEEK_MEMBERSHIP) for number in range(15, 20)
    )
    return week + (
        front_set(date(2024, 1, 22), FEB_C150),
        front_set(date(2024, 1, 23), FEB_C150),
    )


def week_catalog() -> ContractsCatalog:
    static = (JAN_C150, JAN_C155, JAN_P150, FEB_C150, LATE_LISTED, DELISTED)
    return contract_catalog(static, week_front_sets())


def equity_source(
    episodes: tuple[ListingRecord, ...] = ALL_EPISODES,
) -> EquityListingUniverse:
    return EquityListingUniverse(listing_repo(episodes))


def option_source(
    *,
    static_symbols: tuple[str, ...] | None = None,
    catalog: ContractsCatalog | None = None,
) -> OptionContractUniverse:
    return OptionContractUniverse(
        catalog if catalog is not None else week_catalog(),
        "AAPL",
        static_symbols=static_symbols,
    )


def view(
    sources: UniverseSource | Sequence[UniverseSource],
    *,
    head: datetime = HEAD,
    composition: UniverseComposition = UniverseComposition.UNION,
) -> UniverseView:
    return UniverseView(SimClock(head), sources, composition=composition)


class _CountingListings:
    """Spy listings port that records how often the view touched it."""

    def __init__(self, episodes: tuple[ListingRecord, ...]) -> None:
        self._inner = InMemoryListingRepository(episodes)
        self.calls = 0

    def list(
        self,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ListingRecord, ...]:
        self.calls += 1
        return self._inner.list(entity_id=entity_id, symbol=symbol, as_of=as_of)


class _CountingFrontSets:
    """Spy front-set repository recording every requested selection date."""

    def __init__(self, front_sets: tuple[DailyFrontSet, ...]) -> None:
        self._inner = InMemoryDailyFrontSetRepository(front_sets)
        self.requested: list[date] = []

    def get(self, underlying: str, selection_date: date) -> DailyFrontSet | None:
        self.requested.append(selection_date)
        return self._inner.get(underlying, selection_date)

    def upsert(self, front_set: DailyFrontSet) -> DailyFrontSet:
        return self._inner.upsert(front_set)

    def list(
        self,
        *,
        underlying: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[DailyFrontSet, ...]:
        return self._inner.list(underlying=underlying, start=start, end=end)


def test_view_requires_clock_and_sources() -> None:
    with pytest.raises(TypeError, match="clock must implement Clock"):
        UniverseView(cast(Clock, "not-a-clock"), equity_source())
    with pytest.raises(ValueError, match="at least one source"):
        UniverseView(SimClock(HEAD), ())
    with pytest.raises(TypeError, match="sources must implement UniverseSource"):
        UniverseView(
            SimClock(HEAD),
            cast("tuple[UniverseSource, ...]", ("not-a-source",)),
        )


def test_future_as_of_raises_lookahead_error_before_touching_sources() -> None:
    listings = _CountingListings(ALL_EPISODES)
    universe = UniverseView(SimClock(HEAD), EquityListingUniverse(listings))

    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        universe.resolve(datetime(2024, 6, 28, 20, 0, 1, tzinfo=UTC))
    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        universe.symbols(to_nanos(datetime(2025, 1, 2, tzinfo=UTC)))
    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        universe.membership("TSLA", to_nanos(datetime(2025, 1, 2, tzinfo=UTC)))

    assert listings.calls == 0


def test_resolve_defaults_to_head_and_accepts_head_nanoseconds() -> None:
    universe = view(equity_source())

    default_snapshot = universe.resolve()
    explicit_snapshot = universe.resolve(to_nanos(HEAD))

    assert default_snapshot == explicit_snapshot
    assert default_snapshot.as_of == HEAD.date()
    assert default_snapshot.composition is UniverseComposition.UNION


def test_equity_membership_is_point_in_time() -> None:
    universe = view(equity_source())

    assert universe.membership("FUTR") is False
    assert universe.membership("TWTR") is False
    assert universe.membership("META", instant(2020, 1, 2)) is False
    assert universe.membership("FB", instant(2020, 1, 2)) is True
    with pytest.raises(ValueError, match="invalid market symbol"):
        universe.membership("not a symbol")


def test_known_delisting_removes_membership_only_once_it_arrives() -> None:
    before = view(equity_source(), head=instant(2026, 12, 30))
    after = view(equity_source(), head=instant(2027, 1, 4))

    assert before.membership("EARLY") is True
    assert after.membership("EARLY") is False
    assert after.member_metadata("EARLY") is None


def test_rename_yields_exactly_one_member_per_entity() -> None:
    universe = view(equity_source())

    early = universe.resolve(instant(2020, 1, 2))
    late = universe.resolve()

    assert early.symbols == ("FB", "TSLA", "TWTR")
    assert late.symbols == ("EARLY", "META", "TSLA")
    assert len({member.entity_id for member in late}) == len(late)
    meta_member = late.member("META")
    assert meta_member is not None
    assert meta_member.entity_id == "META"


def test_equity_metadata_returns_the_episode_proven_on_the_as_of_date() -> None:
    universe = view(equity_source())

    before = universe.member_metadata("FB", instant(2020, 1, 2))
    after = universe.member_metadata("META")

    assert before is not None and after is not None
    assert before.kind is MemberKind.EQUITY
    assert before.metadata is FB
    assert after.metadata is META
    assert universe.member_metadata("META", instant(2020, 1, 2)) is None
    assert universe.member_metadata("FUTR") is None
    assert universe.member_metadata("ZZZZ") is None


def test_option_membership_follows_the_front_set_roll_and_contract_lifecycle() -> None:
    universe = view(option_source())

    during_week = universe.resolve(instant(2024, 1, 16))
    after_roll = universe.resolve(instant(2024, 1, 22))

    assert during_week.symbols == (
        "AAPL240119C00150000",
        "AAPL240119C00155000",
        "AAPL240119P00150000",
    )
    assert after_roll.symbols == ("AAPL240216C00150000",)
    assert universe.membership("AAPL240119C00150000", instant(2024, 1, 22)) is False
    member = during_week.member("AAPL240119P00150000")
    assert member is not None
    assert member.kind is MemberKind.OPTION
    assert member.entity_id == "AAPL"


def test_option_static_symbols_clip_lifecycle_dated_contracts() -> None:
    universe = view(
        option_source(static_symbols=("AAPL240119C00160000", "AAPL240119C00161000"))
    )

    before_listing = universe.resolve(instant(2024, 1, 16))
    after_listing = universe.resolve(instant(2024, 1, 18))

    assert before_listing.symbols == ()
    assert after_listing.symbols == ("AAPL240119C00160000",)


def test_option_source_only_reads_front_sets_on_or_before_the_engine_date() -> None:
    spy = _CountingFrontSets(week_front_sets())
    catalog = ContractsCatalog(
        InMemoryContractRepository((JAN_C150, FEB_C150)),
        spy,
    )
    universe = view(option_source(catalog=catalog))

    assert universe.symbols(instant(2024, 1, 16)) == (
        "AAPL240119C00150000",
        "AAPL240119C00155000",
        "AAPL240119P00150000",
    )

    assert spy.requested == [date(2024, 1, 16)]


def test_union_composition_includes_equities_and_options() -> None:
    universe = view(
        (equity_source(), option_source()), composition=UniverseComposition.UNION
    )

    snapshot = universe.resolve(instant(2024, 1, 16))

    assert snapshot.equities == ("EARLY", "META", "TSLA")
    assert snapshot.options == (
        "AAPL240119C00150000",
        "AAPL240119C00155000",
        "AAPL240119P00150000",
    )
    assert len(snapshot) == 6
    assert snapshot.provenance == {"total": 6, "equity": 3, "option": 3}


def test_intersection_composition_keeps_only_symbols_in_every_source() -> None:
    left = EquityListingUniverse(listing_repo((TSLA, TWTR, EARLY)))
    right = EquityListingUniverse(listing_repo((TSLA, META)))
    universe = view((left, right), composition=UniverseComposition.INTERSECTION)

    snapshot = universe.resolve()

    assert snapshot.symbols == ("TSLA",)
    assert snapshot.provenance == {"total": 1, "equity": 1}

    disjoint = view(
        (equity_source((TSLA,)), option_source()),
        composition=UniverseComposition.INTERSECTION,
    )
    assert disjoint.symbols() == ()


def test_snapshot_construction_is_the_structural_clipping_chokepoint() -> None:
    expired = UniverseMember(
        "AAPL240119C00150000", MemberKind.OPTION, contract=JAN_C150
    )
    not_yet_listed = UniverseMember("FUTR", MemberKind.EQUITY, listing=FUTURE)

    snapshot = UniverseSnapshot(
        as_of=instant(2024, 1, 22).date(),
        composition=UniverseComposition.UNION,
        members=(not_yet_listed, expired, expired),
    )

    assert snapshot.members == ()
    assert snapshot.provenance == {"total": 0}
    with pytest.raises(TypeError, match="as_of must be a date, not a datetime"):
        UniverseSnapshot(
            as_of=instant(2024, 1, 22),
            composition=UniverseComposition.UNION,
            members=(),
        )
    with pytest.raises(TypeError, match="members must be UniverseMember"):
        UniverseSnapshot(
            as_of=JAN,
            composition=UniverseComposition.UNION,
            members=cast("tuple[UniverseMember, ...]", (JAN_C150,)),
        )


def test_repeated_resolution_is_deterministic() -> None:
    universe = view((equity_source(), option_source()))

    first = universe.resolve(instant(2024, 1, 16))
    second = universe.resolve(instant(2024, 1, 16))

    assert first == second
    assert first.symbols == second.symbols
    assert first.members == second.members


def test_membership_and_member_lookup_reject_invalid_symbols() -> None:
    snapshot = view(equity_source()).resolve()

    assert snapshot.membership("TSLA")
    assert "TSLA" in snapshot
    assert "ZZZZ" not in snapshot
    assert 12 not in snapshot
    with pytest.raises(ValueError, match="invalid market symbol"):
        snapshot.member("not a symbol")


def test_member_requires_exactly_one_matching_record() -> None:
    with pytest.raises(ValueError, match="exactly one record"):
        UniverseMember("TSLA", MemberKind.EQUITY)
    with pytest.raises(TypeError, match="equity members require a ListingRecord"):
        UniverseMember("TSLA", MemberKind.EQUITY, contract=JAN_C150)
    with pytest.raises(TypeError, match="option members require a ContractRecord"):
        UniverseMember("AAPL240119C00150000", MemberKind.OPTION, listing=TSLA)
    with pytest.raises(ValueError, match="exactly one record"):
        UniverseMember("TSLA", MemberKind.EQUITY, listing=TSLA, contract=JAN_C150)


def test_option_source_validates_catalog_static_symbols_and_underlying() -> None:
    with pytest.raises(TypeError, match="catalog must implement"):
        OptionContractUniverse(cast(ContractsCatalog, "not-a-catalog"), "AAPL")
    with pytest.raises(ValueError, match="at least one OSI symbol"):
        option_source(static_symbols=())
    with pytest.raises(ValueError, match="duplicate OSI symbols"):
        option_source(static_symbols=("AAPL240119C00150000",) * 2)
    with pytest.raises(ValueError, match="invalid market symbol"):
        OptionContractUniverse(week_catalog(), "not a symbol")


def test_universe_view_module_avoids_wall_clock_randomness_and_network() -> None:
    source = inspect.getsource(universe_view_module)

    assert "datetime.now" not in source
    assert "import random" not in source
    assert "time.time" not in source
    assert "urlopen" not in source
    assert "requests.get" not in source
    assert "httpx" not in source


# -- dynamic subscriptions: deterministic engine boundary contract ---------


@dataclass(frozen=True, slots=True)
class _Tick:
    """Minimal deterministic event stand-in carrying one symbol."""

    symbol: str
    sequence: int


def stream(*symbols: str) -> tuple[_Tick, ...]:
    return tuple(_Tick(symbol, index) for index, symbol in enumerate(symbols))


class _FakeSubscriptionSet:
    """Fake mirroring the strategy subscription set's filtering contract."""

    def __init__(
        self,
        universe: Iterable[str],
        subscriptions: Iterable[str] | None = None,
    ) -> None:
        self._universe = frozenset(universe)
        self._entries: list[frozenset[str] | None] = []
        if subscriptions is not None:
            self.subscribe(subscriptions)

    def subscriptions(self) -> tuple[frozenset[str] | None, ...]:
        return tuple(self._entries)

    def subscribe(self, symbols: Iterable[str] | str | None = None) -> None:
        entry = self._normalize(symbols)
        if entry not in self._entries:
            self._entries.append(entry)

    def unsubscribe(self, symbols: Iterable[str] | str | None = None) -> None:
        entry = self._normalize(symbols)
        self._entries = [item for item in self._entries if item != entry]

    def is_subscribed(self, symbol: str) -> bool:
        return any(entry is None or symbol in entry for entry in self._entries)

    def _normalize(self, symbols: Iterable[str] | str | None) -> frozenset[str] | None:
        if symbols is None:
            return None
        candidates = (symbols,) if isinstance(symbols, str) else tuple(symbols)
        normalized = frozenset(candidates)
        unknown = normalized - self._universe
        if unknown:
            raise ValueError(
                "subscription symbols must belong to the strategy universe"
            )
        return normalized


class _Shell:
    """One delivery path shared by replay and mock-live engine shells.

    The shell delivers one event at a time in deterministic order and consults
    the live subscription set immediately before each delivery, exactly like
    ``StrategyBinding._receive`` consulting ``StrategyContext._accepts``.
    Callbacks run after the gate and may mutate subscriptions; the mutation is
    therefore in force from the next delivered event onward.
    """

    def __init__(
        self,
        universe: Iterable[str],
        events: Iterable[_Tick],
        subscriptions: Iterable[str] | None = None,
    ) -> None:
        self._set = _FakeSubscriptionSet(universe, subscriptions)
        self._events = events
        self.delivered: list[_Tick] = []

    def run(
        self, on_event: Callable[[_Shell, _Tick], None] | None = None
    ) -> tuple[_Tick, ...]:
        for tick in self._events:
            if not self._set.is_subscribed(tick.symbol):
                continue
            self.delivered.append(tick)
            if on_event is not None:
                on_event(self, tick)
        return tuple(self.delivered)

    def subscribe(self, symbols: Iterable[str] | str | None = None) -> None:
        self._set.subscribe(symbols)

    def unsubscribe(self, symbols: Iterable[str] | str | None = None) -> None:
        self._set.unsubscribe(symbols)


def events() -> tuple[_Tick, ...]:
    return stream("AAA", "BBB", "CCC", "DDD", "EEE")


UNIVERSE = ("AAA", "BBB", "CCC", "DDD", "EEE")


def symbols_of(delivered: Iterable[_Tick]) -> tuple[str, ...]:
    return tuple(tick.symbol for tick in delivered)


def test_subscribe_made_in_a_callback_takes_effect_at_the_next_delivery() -> None:
    shell = _Shell(UNIVERSE, events(), subscriptions=("AAA", "BBB", "CCC"))

    def on_event(shell: _Shell, tick: _Tick) -> None:
        if tick.symbol == "CCC":
            shell.subscribe("DDD")

    delivered = shell.run(on_event)

    assert symbols_of(delivered) == ("AAA", "BBB", "CCC", "DDD")
    assert shell._set.is_subscribed("DDD")


def test_unsubscribe_in_a_callback_cannot_retract_already_gated_events() -> None:
    shell = _Shell(UNIVERSE, events(), subscriptions=("AAA", "BBB", "CCC"))

    def on_event(shell: _Shell, tick: _Tick) -> None:
        if tick.symbol == "CCC":
            shell.unsubscribe(("AAA", "BBB", "CCC"))

    delivered = shell.run(on_event)

    assert symbols_of(delivered) == ("AAA", "BBB", "CCC")
    assert not shell._set.is_subscribed("CCC")
    assert shell._set.subscriptions() == ()


def test_on_start_subscriptions_are_in_force_before_the_first_delivery() -> None:
    shell = _Shell(UNIVERSE, events())
    shell.subscribe(("BBB", "EEE"))

    delivered = shell.run()

    assert symbols_of(delivered) == ("BBB", "EEE")


def test_duplicate_subscribe_is_idempotent_and_missing_unsubscribe_is_harmless() -> (
    None
):
    boundary = _FakeSubscriptionSet(("AAA", "BBB"), subscriptions=("AAA",))

    boundary.subscribe("AAA")
    boundary.subscribe("AAA")
    assert boundary.subscriptions() == (frozenset({"AAA"}),)

    boundary.unsubscribe("BBB")
    assert boundary.subscriptions() == (frozenset({"AAA"}),)
    assert not boundary.is_subscribed("BBB")

    boundary.subscribe(("AAA", "BBB"))
    boundary.unsubscribe("BBB")
    assert boundary.subscriptions() == (
        frozenset({"AAA"}),
        frozenset({"AAA", "BBB"}),
    )
    assert boundary.is_subscribed("BBB")


def test_out_of_universe_subscribe_raises_immediately() -> None:
    boundary = _FakeSubscriptionSet(("AAA", "BBB"))

    with pytest.raises(ValueError, match="strategy universe"):
        boundary.subscribe("ZZZ")
    assert boundary.subscriptions() == ()
    assert not boundary.is_subscribed("AAA")


def test_replay_and_mock_live_shells_produce_identical_event_streams() -> None:
    def replay_events() -> tuple[_Tick, ...]:
        return events()

    def live_events() -> Iterator[_Tick]:
        yield from events()

    def rotation(shell: _Shell, tick: _Tick) -> None:
        if tick.symbol == "CCC":
            shell.subscribe("DDD")
        if tick.symbol == "DDD":
            shell.unsubscribe("AAA")

    replay = _Shell(UNIVERSE, replay_events(), subscriptions=("AAA", "BBB", "CCC"))
    live = _Shell(UNIVERSE, live_events(), subscriptions=("AAA", "BBB", "CCC"))

    replay_stream = replay.run(rotation)
    live_stream = live.run(rotation)

    assert replay_stream == live_stream
    assert symbols_of(replay_stream) == ("AAA", "BBB", "CCC", "DDD")
