"""Focused tests for day-bounded options replay over the lake partition layout."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from harness.core.clock import SimClock
from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent, TradeEvent
from harness.data.history import LookaheadError
from harness.data.options_replay import (
    OptionPartitionPort,
    OptionsReplayFeed,
)
from harness.data.options_universe import (
    OptionUniverseSpec,
    OptionUniverseView,
    SelectionMode,
)
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryDailyFrontSetRepository,
)
from lake.catalog.memory import InMemoryContractRepository
from lake.catalog.models import ContractRecord, OptionType
from lake.contracts import AssetClass as LakeAssetClass
from lake.contracts import DataType as LakeDataType
from lake.contracts import ExchangeSession, partition_path
from lakeclient import AssetClass, DataType, FileQueryFile

JAN_EXPIRY = date(2024, 1, 19)
FEB_EXPIRY = date(2024, 2, 16)
FAR_EXPIRY = date(2026, 12, 18)

SPY_C450 = "SPY240119C00450000"
SPY_P450 = "SPY240119P00450000"
SPY_C460 = "SPY240216C00460000"
SPY_P460 = "SPY240216P00460000"

JAN_FRONT = (SPY_C450, SPY_P450)
FEB_FRONT = (SPY_C460, SPY_P460)

MarketEvent = QuoteEvent | TradeEvent


def session(day: date) -> ExchangeSession:
    start = datetime.combine(day, datetime.min.time(), UTC)
    return ExchangeSession(day, start, start + timedelta(days=1))


def contract(symbol: str, expiry: date) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying="SPY",
        expiration_date=expiry,
        strike_price=Decimal("450"),
        option_type=OptionType.PUT if "P" in symbol[-9:] else OptionType.CALL,
        multiplier=100,
    )


def option_catalog() -> ContractsCatalog:
    contracts = (
        contract(SPY_C450, JAN_EXPIRY),
        contract(SPY_P450, JAN_EXPIRY),
        contract(SPY_C460, FEB_EXPIRY),
        contract(SPY_P460, FEB_EXPIRY),
    )
    january = (
        DailyFrontSet(
            "SPY",
            date(2024, 1, number),
            tuple(contract(symbol, JAN_EXPIRY) for symbol in JAN_FRONT),
        )
        for number in range(15, 20)
    )
    february = (
        DailyFrontSet(
            "SPY",
            date(2024, 1, number),
            tuple(contract(symbol, FEB_EXPIRY) for symbol in FEB_FRONT),
        )
        for number in (22, 23)
    )
    return ContractsCatalog(
        InMemoryContractRepository(contracts),
        InMemoryDailyFrontSetRepository((*january, *february)),
    )


def front_set_spec() -> OptionUniverseSpec:
    return OptionUniverseSpec(underlying="SPY", mode=SelectionMode.FRONT_SET)


def static_spec(*symbols: str) -> OptionUniverseSpec:
    return OptionUniverseSpec(
        underlying="SPY",
        mode=SelectionMode.STATIC_OSI,
        static_symbols=symbols,
    )


class MapPartitionPort:
    """Deterministic in-memory OptionPartitionPort with call recording."""

    def __init__(self, files: Mapping[tuple[date, DataType, str], FileQueryFile]):
        self._files = dict(files)
        self.calls: list[tuple[date, DataType, str]] = []

    def file(
        self, *, session_date: date, data_type: DataType, symbol: str
    ) -> FileQueryFile | None:
        self.calls.append((session_date, data_type, symbol))
        return self._files.get((session_date, data_type, symbol))


class RecordingReader:
    """Deterministic reader returning one recorded event per partition."""

    def __init__(self, events: Mapping[str, MarketEvent]):
        self._events = dict(events)
        self.calls: list[tuple[date, str, DataType]] = []

    def __call__(self, file: FileQueryFile) -> list[MarketEvent]:
        self.calls.append((file.session_date, file.symbol, file.data_type))
        return [self._events[file.object_path]]


def option_file(
    day: date, symbol: str, data_type: DataType, event: MarketEvent
) -> FileQueryFile:
    object_path = partition_path(
        session_date=day,
        asset_class=LakeAssetClass.OPTION,
        data_type=LakeDataType(data_type.value),
        symbol=symbol,
    )
    return FileQueryFile(
        asset_class=AssetClass.OPTION,
        data_type=data_type,
        symbol=symbol,
        session_date=day,
        object_path=object_path,
        url="unused",
        sas_url="unused",
        row_count=1,
        byte_size=128,
        checksum=None,
    )


def option_quote(
    event_id: str, symbol: str, timestamp: datetime, sequence: int
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        timestamp,
        sequence,
        Symbol(symbol),
        Decimal("2.50"),
        Decimal("10"),
        Decimal("2.60"),
        Decimal("10"),
    )


def option_trade(
    event_id: str, symbol: str, timestamp: datetime, sequence: int
) -> TradeEvent:
    return TradeEvent(
        event_id,
        timestamp,
        sequence,
        Symbol(symbol),
        Decimal("2.55"),
        Decimal("5"),
    )


def make_feed(
    days: tuple[date, ...],
    port: MapPartitionPort,
    reader: RecordingReader,
    clock_head: datetime,
    spec: OptionUniverseSpec | None = None,
    **options: Any,
) -> OptionsReplayFeed:
    return OptionsReplayFeed(
        tuple(session(day) for day in days),
        OptionUniverseView(
            SimClock(clock_head),
            option_catalog(),
            spec if spec is not None else front_set_spec(),
        ),
        port,
        reader=reader,
        **options,
    )


def test_front_set_days_plan_and_replay_only_their_own_contracts() -> None:
    days = (date(2024, 1, 17), date(2024, 1, 18), date(2024, 1, 19))
    files: dict[tuple[date, DataType, str], FileQueryFile] = {}
    events: dict[str, MarketEvent] = {}
    expected: list[str] = []
    ordinal = 0
    for day in days:
        for symbol in JAN_FRONT:
            for data_type in (DataType.QUOTE, DataType.TRADE):
                ordinal += 1
                timestamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(
                    hours=14, minutes=30, microseconds=ordinal
                )
                event_id = f"{symbol}-{data_type.value}-{ordinal}"
                event: MarketEvent
                if data_type is DataType.QUOTE:
                    event = option_quote(event_id, symbol, timestamp, ordinal)
                else:
                    event = option_trade(event_id, symbol, timestamp, ordinal)
                files[(day, data_type, symbol)] = option_file(
                    day, symbol, data_type, event
                )
                events[files[(day, data_type, symbol)].object_path] = event
                expected.append(event_id)

    port = MapPartitionPort(files)
    reader = RecordingReader(events)
    assert isinstance(port, OptionPartitionPort)
    feed = make_feed(days, port, reader, datetime(2024, 1, 25, tzinfo=UTC))

    plans = list(feed.days())
    assert [plan.session.session_date for plan in plans] == list(days)
    planning_calls = list(port.calls)
    assert [call[0] for call in planning_calls] == [
        day for day in days for _ in JAN_FRONT for _ in (DataType.QUOTE, DataType.TRADE)
    ]
    for plan in plans:
        assert plan.symbols == JAN_FRONT
        assert len(plan.files) == 4

    assert [event.event_id for event in feed] == expected
    assert [call[0] for call in reader.calls] == [
        day for day in days for _ in JAN_FRONT for _ in (DataType.QUOTE, DataType.TRADE)
    ]


def test_two_year_front_set_run_stays_day_bounded() -> None:
    start = date(2024, 1, 15)
    days = tuple(start + timedelta(days=index) for index in range(504))
    contracts = (
        contract("SPY261218C00450000", FAR_EXPIRY),
        contract("SPY261218P00450000", FAR_EXPIRY),
    )
    symbols = tuple(item.symbol for item in contracts)
    catalog = ContractsCatalog(
        InMemoryContractRepository(contracts),
        InMemoryDailyFrontSetRepository(
            DailyFrontSet("SPY", day, contracts) for day in days
        ),
    )
    files: dict[tuple[date, DataType, str], FileQueryFile] = {}
    events: dict[str, MarketEvent] = {}
    ordinal = 0
    for day in days:
        for symbol in symbols:
            for data_type in (DataType.QUOTE, DataType.TRADE):
                ordinal += 1
                timestamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(
                    hours=14, minutes=30, microseconds=ordinal
                )
                event_id = f"{symbol}-{data_type.value}"
                event: MarketEvent
                if data_type is DataType.QUOTE:
                    event = option_quote(event_id, symbol, timestamp, ordinal)
                else:
                    event = option_trade(event_id, symbol, timestamp, ordinal)
                files[(day, data_type, symbol)] = option_file(
                    day, symbol, data_type, event
                )
                events[files[(day, data_type, symbol)].object_path] = event

    port = MapPartitionPort(files)
    reader = RecordingReader(events)
    feed = OptionsReplayFeed(
        tuple(session(day) for day in days),
        OptionUniverseView(
            SimClock(datetime(2026, 12, 31, tzinfo=UTC)), catalog, front_set_spec()
        ),
        port,
        reader=reader,
    )

    emitted = list(feed)
    assert len(emitted) == 504 * 4
    for event in emitted:
        kind = "quote" if isinstance(event, QuoteEvent) else "trade"
        assert event.event_id == f"{event.symbol}-{kind}"

    reader_days = [call[0] for call in reader.calls]
    assert reader_days == [day for day in days for _ in range(4)]
    port_days = [call[0] for call in port.calls]
    assert port_days == sorted(port_days)
    assert set(port_days) == set(days)


def test_repeated_runs_and_separate_feeds_are_deterministic() -> None:
    days = (date(2024, 1, 17), date(2024, 1, 18))
    files: dict[tuple[date, DataType, str], FileQueryFile] = {}
    events: dict[str, MarketEvent] = {}
    for day in days:
        timestamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(
            hours=14, minutes=30
        )
        event = option_quote(f"{SPY_C450}-quote", SPY_C450, timestamp, 1)
        files[(day, DataType.QUOTE, SPY_C450)] = option_file(
            day, SPY_C450, DataType.QUOTE, event
        )
        events[files[(day, DataType.QUOTE, SPY_C450)].object_path] = event

    def run() -> tuple[OptionsReplayFeed, tuple[MarketEvent, ...]]:
        feed = make_feed(
            days,
            MapPartitionPort(files),
            RecordingReader(events),
            datetime(2024, 1, 25, tzinfo=UTC),
        )
        return feed, tuple(feed)

    first_feed, first_events = run()
    second_feed, second_events = run()
    assert first_events == second_events
    assert first_feed.fingerprints == second_feed.fingerprints
    assert tuple(first_feed) == first_events
    assert first_feed.fingerprints == second_feed.fingerprints
    assert len(first_feed.file_fingerprint) == 64
    assert len(first_feed.row_fingerprint) == 64
    assert len(first_feed.data_fingerprint) == 64


def test_baseline_fingerprints_precede_any_run() -> None:
    days = (date(2024, 1, 17),)
    first = make_feed(
        days,
        MapPartitionPort({}),
        RecordingReader({}),
        datetime(2024, 1, 25, tzinfo=UTC),
    )
    second = make_feed(
        days,
        MapPartitionPort({}),
        RecordingReader({}),
        datetime(2024, 1, 25, tzinfo=UTC),
    )
    baseline = first.fingerprints
    assert second.fingerprints == baseline
    assert list(first) == []
    assert first.fingerprints != baseline
    assert list(second) == []
    assert second.fingerprints == first.fingerprints


def test_planning_ahead_of_the_engine_head_is_a_lookahead_error() -> None:
    days = (date(2024, 1, 17), date(2024, 1, 18))
    feed = OptionsReplayFeed(
        tuple(session(day) for day in days),
        OptionUniverseView(
            SimClock(datetime(2024, 1, 17, tzinfo=UTC)),
            option_catalog(),
            front_set_spec(),
        ),
        MapPartitionPort({}),
        reader=RecordingReader({}),
    )
    feed.plan(session(days[0]))
    with pytest.raises(LookaheadError):
        feed.plan(session(days[1]))


def test_static_osi_selection_clips_expired_contracts() -> None:
    days = (date(2024, 1, 22),)
    port = MapPartitionPort({})
    feed = make_feed(
        days,
        port,
        RecordingReader({}),
        datetime(2024, 1, 25, tzinfo=UTC),
        spec=static_spec(SPY_C450, SPY_C460),
    )
    plan = feed.plan(session(days[0]))
    assert plan.symbols == (SPY_C460,)
    requested = [call[2] for call in port.calls]
    assert requested == [SPY_C460, SPY_C460]


def test_missing_partitions_are_skipped_deterministically() -> None:
    day = date(2024, 1, 17)
    timestamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=14)
    event = option_quote("quote", SPY_C450, timestamp, 1)
    present = option_file(day, SPY_C450, DataType.QUOTE, event)
    port = MapPartitionPort({(day, DataType.QUOTE, SPY_C450): present})
    feed = make_feed(
        (day,),
        port,
        RecordingReader({present.object_path: event}),
        datetime(2024, 1, 25, tzinfo=UTC),
    )
    plan = feed.plan(session(day))
    assert plan.files == (present,)
    assert [event.event_id for event in feed] == ["quote"]


def test_underlying_streams_merge_with_option_files() -> None:
    day = date(2024, 1, 17)
    midnight = datetime.combine(day, datetime.min.time(), UTC)
    option_event = option_quote(
        "option-quote", SPY_C450, midnight + timedelta(hours=14, minutes=30), 2
    )
    underlying = QuoteEvent(
        "underlying-quote",
        midnight + timedelta(hours=14, minutes=29),
        1,
        Symbol("SPY"),
        Decimal("450.00"),
        Decimal("100"),
        Decimal("450.05"),
        Decimal("100"),
    )
    partition = option_file(day, SPY_C450, DataType.QUOTE, option_event)
    port = MapPartitionPort({(day, DataType.QUOTE, SPY_C450): partition})
    feed = make_feed(
        (day,),
        port,
        RecordingReader({partition.object_path: option_event}),
        datetime(2024, 1, 25, tzinfo=UTC),
        quote_streams={"SPY": [underlying]},
    )
    assert [event.event_id for event in feed] == ["underlying-quote", "option-quote"]


def test_non_option_partition_is_rejected() -> None:
    day = date(2024, 1, 17)
    equity_file = FileQueryFile(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbol=SPY_C450,
        session_date=day,
        object_path=partition_path(
            session_date=day,
            asset_class=LakeAssetClass.EQUITY,
            data_type=LakeDataType.QUOTE,
            symbol=SPY_C450,
        ),
        url="unused",
        sas_url="unused",
        row_count=1,
        byte_size=128,
    )
    port = MapPartitionPort({(day, DataType.QUOTE, SPY_C450): equity_file})
    feed = make_feed(
        (day,), port, RecordingReader({}), datetime(2024, 1, 25, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="not an option partition"):
        feed.plan(session(day))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param("data_type", "partition, not quote"),
        pytest.param("session_date", "belongs to"),
        pytest.param("symbol", "belongs to"),
    ],
)
def test_mismatched_partition_metadata_is_rejected(mutate: str, message: str) -> None:
    day = date(2024, 1, 17)
    file = FileQueryFile(
        asset_class=AssetClass.OPTION,
        data_type=DataType.TRADE if mutate == "data_type" else DataType.QUOTE,
        symbol=SPY_P450 if mutate == "symbol" else SPY_C450,
        session_date=date(2024, 1, 18) if mutate == "session_date" else day,
        object_path=partition_path(
            session_date=day,
            asset_class=LakeAssetClass.OPTION,
            data_type=LakeDataType.QUOTE,
            symbol=SPY_C450,
        ),
        url="unused",
        sas_url="unused",
        row_count=1,
        byte_size=128,
    )
    port = MapPartitionPort({(day, DataType.QUOTE, SPY_C450): file})
    feed = make_feed(
        (day,), port, RecordingReader({}), datetime(2024, 1, 25, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match=message):
        feed.plan(session(day))


def test_session_and_data_type_validation() -> None:
    view = OptionUniverseView(
        SimClock(datetime(2024, 1, 25, tzinfo=UTC)),
        option_catalog(),
        front_set_spec(),
    )
    with pytest.raises(ValueError, match="at least one replay session"):
        OptionsReplayFeed((), view, MapPartitionPort({}), reader=RecordingReader({}))
    with pytest.raises(ValueError, match="duplicate dates"):
        OptionsReplayFeed(
            (session(date(2024, 1, 17)), session(date(2024, 1, 17))),
            view,
            MapPartitionPort({}),
            reader=RecordingReader({}),
        )
    with pytest.raises(TypeError, match="reader is required"):
        OptionsReplayFeed(
            (session(date(2024, 1, 17)),),
            view,
            MapPartitionPort({}),
            **{"reader": None},  # ty: ignore[invalid-argument-type]
        )
    unsorted = OptionsReplayFeed(
        (session(date(2024, 1, 18)), session(date(2024, 1, 17))),
        view,
        MapPartitionPort({}),
        reader=RecordingReader({}),
    )
    assert [day.session_date for day in unsorted.sessions] == [
        date(2024, 1, 17),
        date(2024, 1, 18),
    ]
    with pytest.raises(ValueError, match="not part of this options replay"):
        unsorted.plan(session(date(2024, 1, 25)))


def test_data_types_are_normalized_and_filter_partitions() -> None:
    day = date(2024, 1, 17)
    port = MapPartitionPort({})
    both = make_feed(
        (day,),
        port,
        RecordingReader({}),
        datetime(2024, 1, 25, tzinfo=UTC),
        data_types=("trade", "quote"),
    )
    assert both.data_types == (DataType.QUOTE, DataType.TRADE)
    both.plan(session(day))
    assert [call[1] for call in port.calls] == [
        DataType.QUOTE,
        DataType.TRADE,
        DataType.QUOTE,
        DataType.TRADE,
    ]

    quote_only_port = MapPartitionPort({})
    quote_only = make_feed(
        (day,),
        quote_only_port,
        RecordingReader({}),
        datetime(2024, 1, 25, tzinfo=UTC),
        data_types=(DataType.QUOTE,),
    )
    quote_only.plan(session(day))
    assert [call[1] for call in quote_only_port.calls] == [
        DataType.QUOTE,
        DataType.QUOTE,
    ]

    with pytest.raises(ValueError):
        make_feed(
            (day,),
            MapPartitionPort({}),
            RecordingReader({}),
            datetime(2024, 1, 25, tzinfo=UTC),
            data_types=("bar",),
        )
    with pytest.raises(ValueError, match="at least one"):
        make_feed(
            (day,),
            MapPartitionPort({}),
            RecordingReader({}),
            datetime(2024, 1, 25, tzinfo=UTC),
            data_types=(),
        )
