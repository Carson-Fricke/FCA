"""Focused tests for the point-in-time option contract universe."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

import harness.data.options_universe as options_universe_module
from harness.core.clock import Clock, SimClock
from harness.data.history import LookaheadError, to_nanos
from harness.data.options_universe import (
    ContractsCatalogPort,
    DteFilter,
    MoneynessFilter,
    OptionUniverseSnapshot,
    OptionUniverseSpec,
    OptionUniverseView,
    SelectionMode,
    StrikeFilter,
    UnderlyingView,
)
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryDailyFrontSetRepository,
)
from lake.catalog.memory import InMemoryContractRepository
from lake.catalog.models import ContractRecord, OptionType

JAN = date(2024, 1, 19)
FEB = date(2024, 2, 16)
MAR = date(2024, 3, 15)


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


JAN_C150 = contract("AAPL240119C00150000")
JAN_C151 = contract("AAPL240119C00151000", strike="151")
JAN_C155 = contract("AAPL240119C00155000", strike="155")
JAN_C160 = contract("AAPL240119C00160000", strike="160")
JAN_P150 = contract("AAPL240119P00150000", right=OptionType.PUT)
FEB_C150 = contract("AAPL240216C00150000", expiry=FEB)
FEB_C155 = contract("AAPL240216C00155000", expiry=FEB, strike="155")
MAR_C150 = contract("AAPL240315C00150000", expiry=MAR)

WEEK_CONTRACTS = (
    JAN_C150,
    JAN_C151,
    JAN_C155,
    JAN_C160,
    JAN_P150,
    FEB_C150,
    FEB_C155,
)
WEEK_MEMBERSHIP = (JAN_C150, JAN_C155, JAN_P150, FEB_C150, FEB_C155)


def day(day_of_month: int) -> datetime:
    return datetime(2024, 1, day_of_month, 14, 30, tzinfo=UTC)


def catalog(
    contracts: tuple[ContractRecord, ...] = (),
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


def expiry_week_catalog() -> ContractsCatalog:
    week = tuple(
        front_set(date(2024, 1, number), *WEEK_MEMBERSHIP) for number in range(15, 20)
    )
    after = (
        front_set(date(2024, 1, 22), FEB_C150, FEB_C155),
        front_set(date(2024, 1, 23), FEB_C150, FEB_C155),
    )
    return catalog(WEEK_CONTRACTS, week + after)


def front_spec(
    *,
    dte: DteFilter | None = None,
    strike: StrikeFilter | None = None,
    moneyness: MoneynessFilter | None = None,
) -> OptionUniverseSpec:
    return OptionUniverseSpec(
        underlying="AAPL",
        mode=SelectionMode.FRONT_SET,
        dte=dte,
        strike=strike,
        moneyness=moneyness,
    )


def static_spec(
    *symbols: str,
    dte: DteFilter | None = None,
    strike: StrikeFilter | None = None,
    moneyness: MoneynessFilter | None = None,
) -> OptionUniverseSpec:
    return OptionUniverseSpec(
        underlying="AAPL",
        mode=SelectionMode.STATIC_OSI,
        static_symbols=symbols,
        dte=dte,
        strike=strike,
        moneyness=moneyness,
    )


class StubUnderlyings:
    """In-memory underlying reference data keyed by engine date."""

    def __init__(self, prices: Mapping[date, Decimal], *, listed: bool = True) -> None:
        self._prices = dict(prices)
        self.listed = listed

    def is_listed(self, underlying: str, as_of: date) -> bool:
        return self.listed

    def reference_price(self, underlying: str, as_of: date) -> Decimal | None:
        return self._prices.get(as_of)


def test_expiry_week_resolves_daily_contracts_and_retires_after_expiry() -> None:
    clock = SimClock(day(15))
    view = OptionUniverseView(clock, expiry_week_catalog(), front_spec())

    monday = view.resolve()
    assert monday.as_of == date(2024, 1, 15)
    assert monday.front_expiry == JAN
    assert monday.dte(JAN) == 4
    assert monday.symbols == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00155000",
        "AAPL240216C00150000",
        "AAPL240216C00155000",
    )

    clock.advance_to(day(18))
    thursday = view.resolve()
    assert thursday.dte(JAN) == 1
    assert "AAPL240119C00150000" in thursday

    clock.advance_to(day(19))
    friday = view.resolve()
    assert friday.dte(JAN) == 0
    assert friday.front == (JAN_C150, JAN_P150, JAN_C155)

    clock.advance_to(day(22))
    retired = view.resolve()
    assert retired.front_expiry == FEB
    assert retired.at_expiry(JAN) == ()
    assert "AAPL240119C00150000" not in retired
    assert retired.symbols == ("AAPL240216C00150000", "AAPL240216C00155000")


def test_stale_front_set_membership_is_clipped_structurally() -> None:
    stale = front_set(
        date(2024, 1, 22),
        JAN_C150,
        contract(
            "AAPL240216C00160000",
            expiry=FEB,
            strike="160",
            listed=date(2024, 1, 23),
        ),
        FEB_C150,
    )
    view = OptionUniverseView(
        SimClock(day(22)), catalog(WEEK_CONTRACTS, (stale,)), front_spec()
    )

    snapshot = view.resolve()

    assert snapshot.symbols == ("AAPL240216C00150000",)


def test_front_set_mode_never_reads_future_membership() -> None:
    wednesday = front_set(date(2024, 1, 17), JAN_C150)
    thursday = front_set(date(2024, 1, 18), JAN_C150, JAN_C155)
    rolled = front_set(date(2024, 1, 22), FEB_C150)
    clock = SimClock(day(16))
    view = OptionUniverseView(
        clock, catalog(WEEK_CONTRACTS, (wednesday, thursday, rolled)), front_spec()
    )

    assert view.resolve().contracts == ()

    clock.advance_to(day(17))
    assert view.resolve().symbols == ("AAPL240119C00150000",)

    clock.advance_to(day(18))
    assert view.resolve().symbols == (
        "AAPL240119C00150000",
        "AAPL240119C00155000",
    )

    clock.advance_to(day(21))
    assert view.resolve().contracts == ()


def test_static_mode_selects_named_contracts_point_in_time() -> None:
    late_listing = contract("AAPL240315C00150000", expiry=MAR, listed=date(2024, 1, 17))
    early_exit = contract("AAPL240119C00150000", delisted=date(2024, 1, 16))
    book = catalog((late_listing, early_exit, JAN_C155))
    clock = SimClock(day(15))
    view = OptionUniverseView(
        clock,
        book,
        static_spec(
            "AAPL240119C00150000",
            "AAPL240119C00155000",
            "AAPL240315C00150000",
            "AAPL240419C00200000",
        ),
    )

    assert view.resolve().symbols == ("AAPL240119C00150000", "AAPL240119C00155000")

    clock.advance_to(day(17))
    assert view.resolve().symbols == ("AAPL240119C00155000", "AAPL240315C00150000")

    clock.advance_to(day(18))
    assert view.resolve().symbols == ("AAPL240119C00155000", "AAPL240315C00150000")


def test_static_and_front_set_modes_differ_on_the_same_catalog() -> None:
    week = (JAN_C150, FEB_C150, FEB_C155, MAR_C150)
    book = catalog(
        week,
        tuple(
            front_set(date(2024, 1, number), JAN_C150, FEB_C150, FEB_C155)
            for number in range(15, 20)
        )
        + (front_set(date(2024, 1, 22), FEB_C150, FEB_C155),),
    )
    clock = SimClock(day(22))
    front_view = OptionUniverseView(clock, book, front_spec())
    static_view = OptionUniverseView(
        clock,
        book,
        static_spec(
            "AAPL240119C00150000", "AAPL240216C00150000", "AAPL240315C00150000"
        ),
    )

    assert front_view.resolve().symbols == (
        "AAPL240216C00150000",
        "AAPL240216C00155000",
    )
    assert static_view.resolve().symbols == (
        "AAPL240216C00150000",
        "AAPL240315C00150000",
    )

    clock.advance_to(datetime(2024, 2, 20, 14, 30, tzinfo=UTC))
    assert front_view.resolve().contracts == ()
    assert static_view.resolve().symbols == ("AAPL240315C00150000",)


def test_dte_filter_is_evaluated_against_the_engine_date() -> None:
    clock = SimClock(day(16))
    book = expiry_week_catalog()
    view = OptionUniverseView(clock, book, front_spec(dte=DteFilter(max_days=2)))

    assert view.resolve().contracts == ()

    clock.advance_to(day(17))
    assert view.resolve().symbols == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00155000",
    )

    windowed = OptionUniverseView(
        clock, book, front_spec(dte=DteFilter(min_days=1, max_days=3))
    )
    assert len(windowed.resolve()) == 3

    clock.advance_to(day(19))
    assert view.resolve().symbols == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00155000",
    )
    assert windowed.resolve().contracts == ()


def test_strike_filter_bounds_are_inclusive() -> None:
    book = catalog(WEEK_CONTRACTS, (front_set(date(2024, 1, 16), *WEEK_CONTRACTS),))
    view = OptionUniverseView(
        SimClock(day(16)), book, front_spec(strike=StrikeFilter("150", "155"))
    )

    assert view.resolve().symbols == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00151000",
        "AAPL240119C00155000",
        "AAPL240216C00150000",
        "AAPL240216C00155000",
    )

    upper_only = OptionUniverseView(
        SimClock(day(16)), book, front_spec(strike=StrikeFilter(min_strike="156"))
    )
    assert upper_only.resolve().symbols == ("AAPL240119C00160000",)


def test_moneyness_filter_uses_the_as_of_reference_price() -> None:
    book = catalog(WEEK_CONTRACTS, (front_set(date(2024, 1, 16), *WEEK_CONTRACTS),))
    underlyings = StubUnderlyings(
        {date(2024, 1, 16): Decimal("151"), date(2024, 1, 17): Decimal("149")}
    )
    clock = SimClock(day(16))
    view = OptionUniverseView(
        clock,
        book,
        front_spec(moneyness=MoneynessFilter(max_moneyness="1.0")),
        underlyings=underlyings,
    )

    assert view.resolve().symbols == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00151000",
        "AAPL240216C00150000",
    )

    clock.advance_to(day(17))
    assert view.resolve().contracts == ()

    band = OptionUniverseView(
        SimClock(day(16)),
        book,
        front_spec(moneyness=MoneynessFilter("0.99", "1.0")),
        underlyings=underlyings,
    )
    assert band.resolve().symbols == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00151000",
        "AAPL240216C00150000",
    )

    clock.advance_to(day(18))
    assert view.resolve().contracts == ()


def test_moneyness_filter_requires_an_underlying_view() -> None:
    with pytest.raises(ValueError, match="moneyness filters require"):
        OptionUniverseView(
            SimClock(day(16)),
            expiry_week_catalog(),
            front_spec(moneyness=MoneynessFilter(max_moneyness="1.0")),
        )


def test_underlying_join_hides_contracts_when_the_underlying_is_unlisted() -> None:
    book = expiry_week_catalog()
    clock = SimClock(day(16))
    spec = front_spec()

    listed = OptionUniverseView(
        clock, book, spec, underlyings=StubUnderlyings({}, listed=True)
    )
    assert "AAPL240119C00150000" in listed.resolve()

    unlisted = OptionUniverseView(
        clock, book, spec, underlyings=StubUnderlyings({}, listed=False)
    )
    assert unlisted.resolve().contracts == ()
    assert unlisted.resolve().front_expiry is None


def test_resolve_clips_to_the_engine_clock_head() -> None:
    clock = SimClock(day(17))
    view = OptionUniverseView(clock, expiry_week_catalog(), front_spec())

    past = view.resolve(day(16))
    assert past.as_of == date(2024, 1, 16)
    assert past.contracts == view.resolve(to_nanos(day(16))).contracts
    assert view.resolve().as_of == date(2024, 1, 17)

    with pytest.raises(LookaheadError, match="newer than the engine clock head"):
        view.resolve(day(18))

    with pytest.raises(ValueError, match="must be timezone-aware"):
        view.resolve(datetime(2024, 1, 16, 14, 30))


def test_snapshot_indexes_contracts_by_expiry() -> None:
    clock = SimClock(day(16))
    view = OptionUniverseView(clock, expiry_week_catalog(), front_spec())
    snapshot = view.resolve()

    assert snapshot.expiries == (JAN, FEB)
    assert snapshot.front_expiry == JAN
    assert snapshot.front == (JAN_C150, JAN_P150, JAN_C155)
    assert len(snapshot) == 5
    assert tuple(record.symbol for record in snapshot) == snapshot.symbols
    assert "AAPL240216C00155000" in snapshot
    assert "AAPL240119C00160000" not in snapshot
    assert snapshot.contract("aapl240119c00150000") == JAN_C150
    assert snapshot.contract("AAPL240119C00160000") is None
    assert tuple(record.symbol for record in snapshot.at_expiry(JAN)) == (
        "AAPL240119C00150000",
        "AAPL240119P00150000",
        "AAPL240119C00155000",
    )
    assert snapshot.at_expiry(MAR) == ()
    with pytest.raises(TypeError, match="expiry must be a date"):
        snapshot.at_expiry(cast("date", day(16)))

    index = snapshot.by_expiry()
    assert set(index) == {JAN, FEB}
    with pytest.raises(TypeError):
        cast("dict[date, tuple[ContractRecord, ...]]", index)[FEB] = ()


def test_snapshot_clips_invalid_and_unselected_contracts() -> None:
    with pytest.raises(TypeError, match="as_of must be a date"):
        OptionUniverseSnapshot(
            underlying="AAPL",
            as_of=cast("date", day(16)),
            mode=SelectionMode.FRONT_SET,
            contracts=(),
        )

    spy = ContractRecord(
        symbol="SPY240119C00500000",
        underlying="SPY",
        expiration_date=JAN,
        strike_price=Decimal("500"),
        option_type=OptionType.CALL,
    )
    snapshot = OptionUniverseSnapshot(
        underlying="AAPL",
        as_of=date(2024, 1, 22),
        mode=SelectionMode.STATIC_OSI,
        contracts=(JAN_C150, FEB_C150, spy),
    )

    assert snapshot.symbols == ("AAPL240216C00150000",)
    assert snapshot == OptionUniverseSnapshot(
        underlying="aapl",
        as_of=date(2024, 1, 22),
        mode=SelectionMode.STATIC_OSI,
        contracts=(FEB_C150,),
    )


def test_resolve_is_repeatable_and_leaves_the_catalog_unchanged() -> None:
    book = expiry_week_catalog()
    view = OptionUniverseView(SimClock(day(16)), book, front_spec())

    first = view.resolve()
    second = view.resolve()

    assert first == second
    assert len(book.list_front_sets(underlying="AAPL")) == 7
    assert len(book.list(underlying="AAPL")) == len(WEEK_CONTRACTS)


def test_spec_and_filter_validation() -> None:
    with pytest.raises(ValueError, match="duplicate OSI symbols"):
        static_spec("AAPL240119C00150000", "AAPL240119C00150000")
    with pytest.raises(ValueError, match="requires at least one static symbol"):
        static_spec()
    with pytest.raises(ValueError, match="must not name static symbols"):
        OptionUniverseSpec("AAPL", SelectionMode.FRONT_SET, ("AAPL240119C00150000",))
    with pytest.raises(ValueError, match="invalid OSI symbol"):
        static_spec("NOT-OSI")
    with pytest.raises(ValueError, match="min_days must not exceed max_days"):
        DteFilter(min_days=5, max_days=2)
    with pytest.raises(ValueError, match="non-negative"):
        DteFilter(min_days=-1)
    with pytest.raises(ValueError, match="min_strike must not exceed max_strike"):
        StrikeFilter("160", "150")
    with pytest.raises(ValueError, match="max_strike must be positive"):
        StrikeFilter(max_strike="0")
    with pytest.raises(ValueError, match="min_moneyness must not exceed max_moneyness"):
        MoneynessFilter("2", "1")
    with pytest.raises(TypeError, match="dte must be a DteFilter"):
        OptionUniverseSpec("AAPL", SelectionMode.FRONT_SET, dte=cast("DteFilter", "1d"))


def test_view_construction_validation() -> None:
    book = expiry_week_catalog()
    spec = front_spec()

    with pytest.raises(TypeError, match="clock must implement Clock"):
        OptionUniverseView(cast("Clock", object()), book, spec)
    with pytest.raises(TypeError, match="catalog must implement"):
        OptionUniverseView(
            SimClock(day(16)), cast("ContractsCatalogPort", object()), spec
        )
    with pytest.raises(TypeError, match="spec must be an OptionUniverseSpec"):
        OptionUniverseView(
            SimClock(day(16)), book, cast("OptionUniverseSpec", object())
        )
    with pytest.raises(TypeError, match="underlyings must implement"):
        OptionUniverseView(
            SimClock(day(16)),
            book,
            front_spec(moneyness=MoneynessFilter(max_moneyness="1.0")),
            underlyings=cast("UnderlyingView", object()),
        )


def test_options_universe_module_uses_no_wall_clock_random_or_network_source() -> None:
    source = inspect.getsource(options_universe_module)
    forbidden = (
        "datetime.now",
        "utcnow",
        "time.time",
        "monotonic",
        "perf_counter",
        "import random",
        "random.",
        "secrets",
        "socket",
        "urllib",
        "httpx",
        "requests",
        "open(",
    )

    assert tuple(token for token in forbidden if token in source) == ()
    assert not any(
        name.startswith(("urllib", "socket", "http"))
        for name in dir(options_universe_module)
    )
