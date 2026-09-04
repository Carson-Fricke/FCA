"""Focused tests for the live event recorder in lake schemas."""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from harness.core.clock import Clock, SimClock
from harness.core.symbols import Symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.data.live_recorder import (
    LiveObjectStore,
    LiveRecorder,
    LiveRecorderConfig,
    LocalLiveObjectStore,
    SessionResolver,
    UtcDaySessionResolver,
    XnysSessionResolver,
    live_part_path,
)
from lake.contracts import AssetClass, DataType, schema_for

STAMP = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


@dataclass
class FakeStore:
    """In-memory store recording whole-object replacements."""

    objects: dict[str, bytes] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Replace one object with ``payload``."""
        self.objects[relative_path] = payload
        self.writes.append(relative_path)


class StaticResolver:
    """Pins every timestamp to one session date for injection tests."""

    __slots__ = ("_session_date",)

    def __init__(self, value: date) -> None:
        self._session_date = value

    def session_date(self, moment: datetime) -> date:
        """Pin ``moment`` to one session date."""
        return self._session_date


def _quote(
    event_id: str,
    *,
    offset: int,
    sequence: int,
    symbol: str = "SPY",
    venue: str | None = "XNAS",
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol(symbol),
        Decimal("100.00"),
        Decimal("1"),
        Decimal("100.02"),
        Decimal("2"),
        venue,
    )


def _trade(
    event_id: str,
    *,
    offset: int,
    sequence: int,
    symbol: str = "SPY",
    venue: str | None = "XNAS",
    trade_id: str | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol(symbol),
        Decimal("100.01"),
        Decimal("2"),
        venue,
        str(sequence) if trade_id is None else trade_id,
    )


def _crypto_trade(
    event_id: str,
    *,
    offset: int = 0,
    sequence: int = 1,
    symbol: str = "BTC/USD",
    venue: str | None = None,
    trade_id: str | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol(symbol),
        Decimal("42000.50"),
        Decimal("0.25"),
        venue,
        trade_id,
    )


def _crypto_quote(
    event_id: str,
    *,
    offset: int = 0,
    sequence: int = 1,
    symbol: str = "BTC/USD",
    venue: str | None = None,
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol(symbol),
        Decimal("42000.10"),
        Decimal("0.001"),
        Decimal("42000.90"),
        Decimal("0.002"),
        venue,
    )


def _config(
    store: LiveObjectStore | str,
    *,
    deployment: str = "paper",
    asset_class: AssetClass = AssetClass.EQUITY,
    clock: SimClock | None = None,
    batch_size: int = 2,
    flush_interval: timedelta | None = None,
    resolver: SessionResolver | None = None,
) -> LiveRecorderConfig:
    return LiveRecorderConfig(
        deployment=deployment,
        asset_class=asset_class,
        store=store,
        clock=clock if clock is not None else SimClock(STAMP),
        batch_size=batch_size,
        flush_interval=flush_interval,
        session_resolver=resolver,
    )


def _read_parts(
    store: FakeStore, asset_class: AssetClass, data_type: DataType
) -> list[pa.Table]:
    expected = schema_for(asset_class, data_type)
    tables: list[pa.Table] = []
    for path in sorted(store.objects):
        if f"data_type={data_type.value}/" not in path:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            table = pq.read_table(io.BytesIO(store.objects[path]))
        assert table.schema.equals(expected, check_metadata=True)
        tables.append(table)
    return tables


def test_thirty_minute_recording_replays_without_schema_warnings() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=7))
    quotes = 0
    trades = 0
    for minute in range(30):
        moment = STAMP + timedelta(minutes=minute)
        recorder.record(
            QuoteEvent(
                f"q-{minute}",
                moment,
                minute,
                Symbol("SPY"),
                Decimal("100.00"),
                Decimal("1"),
                Decimal("100.02"),
                Decimal("2"),
                "XNAS",
            )
        )
        quotes += 1
        recorder.record(
            TradeEvent(
                f"t-{minute}",
                moment,
                minute,
                Symbol("QQQ"),
                Decimal("200.01"),
                Decimal("3"),
                "XNAS",
                str(minute),
            )
        )
        trades += 1
    recorder.close()

    quote_tables = _read_parts(store, AssetClass.EQUITY, DataType.QUOTE)
    trade_tables = _read_parts(store, AssetClass.EQUITY, DataType.TRADE)
    assert sum(table.num_rows for table in quote_tables) == quotes
    assert sum(table.num_rows for table in trade_tables) == trades
    for path in sorted(store.objects):
        assert path.startswith("live/paper/date=2024-01-02/asset_class=equity/")
        assert ".part-" in path
    for tables in (quote_tables, trade_tables):
        for table in tables:
            stamps = table.column("timestamp").to_pylist()
            assert stamps == sorted(stamps)
    for table in quote_tables:
        assert set(table.column("symbol").to_pylist()) == {"SPY"}


def test_crypto_rotation_uses_utc_days_and_keeps_parts_readable() -> None:
    store = FakeStore()
    recorder = LiveRecorder(
        _config(store, asset_class=AssetClass.CRYPTO, batch_size=100)
    )
    recorder.record(
        TradeEvent(
            "day-one",
            datetime(2024, 1, 1, 23, 59, 59, tzinfo=UTC),
            1,
            Symbol("BTC/USD"),
            Decimal("42000.50"),
            Decimal("0.25"),
            None,
            None,
        )
    )
    recorder.record(
        TradeEvent(
            "day-two",
            datetime(2024, 1, 2, 0, 1, tzinfo=UTC),
            2,
            Symbol("BTC/USD"),
            Decimal("42001.00"),
            Decimal("0.25"),
            None,
            None,
        )
    )

    assert sorted(store.objects) == [
        "live/paper/date=2024-01-01/asset_class=crypto/data_type=trade/"
        "BTC%2FUSD.part-000000.parquet",
    ]
    trade_tables = _read_parts(store, AssetClass.CRYPTO, DataType.TRADE)
    assert [table.num_rows for table in trade_tables] == [1]
    assert not recorder.closed
    recorder.close()
    assert sorted(store.objects) == [
        "live/paper/date=2024-01-01/asset_class=crypto/data_type=trade/"
        "BTC%2FUSD.part-000000.parquet",
        "live/paper/date=2024-01-02/asset_class=crypto/data_type=trade/"
        "BTC%2FUSD.part-000000.parquet",
    ]
    assert [
        table.num_rows
        for table in _read_parts(store, AssetClass.CRYPTO, DataType.TRADE)
    ] == [1, 1]


def test_earlier_session_event_is_rejected_after_rotation() -> None:
    store = FakeStore()
    recorder = LiveRecorder(
        _config(store, asset_class=AssetClass.CRYPTO, batch_size=100)
    )
    recorder.record(_crypto_trade("first"))
    recorder.record(
        TradeEvent(
            "second",
            datetime(2024, 1, 2, 0, 1, tzinfo=UTC),
            2,
            Symbol("BTC/USD"),
            Decimal("42001"),
            Decimal("0.25"),
            None,
            None,
        )
    )
    with pytest.raises(ValueError, match="precedes the active session"):
        recorder.record(
            TradeEvent(
                "late",
                datetime(2024, 1, 1, 23, 59, tzinfo=UTC),
                3,
                Symbol("BTC/USD"),
                Decimal("42002"),
                Decimal("0.25"),
                None,
                None,
            )
        )
    recorder.record(_crypto_quote("after", offset=0, sequence=4))
    recorder.close()

    assert recorder.closed
    trade_tables = _read_parts(store, AssetClass.CRYPTO, DataType.TRADE)
    assert sum(table.num_rows for table in trade_tables) == 2


def test_equity_rotation_follows_xnys_session_dates() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=100))
    recorder.record(
        TradeEvent(
            "friday",
            datetime(2024, 1, 5, 15, 0, tzinfo=UTC),
            1,
            Symbol("SPY"),
            Decimal("100.01"),
            Decimal("2"),
            "XNAS",
            "1",
        )
    )
    recorder.record(
        TradeEvent(
            "monday",
            datetime(2024, 1, 8, 14, 31, tzinfo=UTC),
            2,
            Symbol("SPY"),
            Decimal("100.02"),
            Decimal("3"),
            "XNAS",
            "2",
        )
    )

    assert sorted(path.split("/")[2] for path in store.objects) == [
        "date=2024-01-05",
    ]
    trade_tables = _read_parts(store, AssetClass.EQUITY, DataType.TRADE)
    assert [table.num_rows for table in trade_tables] == [1]
    recorder.close()
    assert sorted(path.split("/")[2] for path in store.objects) == [
        "date=2024-01-05",
        "date=2024-01-08",
    ]
    assert [
        table.num_rows
        for table in _read_parts(store, AssetClass.EQUITY, DataType.TRADE)
    ] == [1, 1]


def test_xnys_resolver_attributes_extended_hours_and_gaps() -> None:
    resolver = XnysSessionResolver()
    assert resolver.session_date(datetime(2024, 1, 2, 14, 30, tzinfo=UTC)) == date(
        2024, 1, 2
    )
    assert resolver.session_date(datetime(2024, 1, 2, 21, 30, tzinfo=UTC)) == date(
        2024, 1, 2
    )
    assert resolver.session_date(datetime(2024, 1, 9, 13, 0, tzinfo=UTC)) == date(
        2024, 1, 9
    )
    assert resolver.session_date(datetime(2024, 1, 6, 17, 0, tzinfo=UTC)) == date(
        2024, 1, 5
    )
    assert resolver.session_date(datetime(2024, 1, 6, 18, 0, tzinfo=UTC)) == date(
        2024, 1, 5
    )
    assert resolver.session_date(datetime(2024, 1, 7, 7, 0, tzinfo=UTC)) == date(
        2024, 1, 8
    )
    assert resolver.session_date(datetime(2024, 1, 15, 18, 0, tzinfo=UTC)) == date(
        2024, 1, 16
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        resolver.session_date(datetime(2024, 1, 2, 15, 0))


def test_utc_day_resolver_uses_utc_dates() -> None:
    resolver = UtcDaySessionResolver()

    assert resolver.session_date(datetime(2024, 1, 1, 23, 59, 59, tzinfo=UTC)) == date(
        2024, 1, 1
    )
    assert resolver.session_date(datetime(2024, 1, 2, 0, 0, tzinfo=UTC)) == date(
        2024, 1, 2
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        resolver.session_date(datetime(2024, 1, 2, 12, 0))


def test_partial_output_is_readable_before_shutdown() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=2))
    recorder.record(_quote("q1", offset=0, sequence=1))
    recorder.record(_quote("q2", offset=1, sequence=2))

    tables = _read_parts(store, AssetClass.EQUITY, DataType.QUOTE)
    assert [table.num_rows for table in tables] == [2]
    assert not recorder.closed

    recorder.record(_trade("t1", offset=2, sequence=3))
    recorder.record(_trade("t2", offset=3, sequence=4))
    assert [
        table.num_rows
        for table in _read_parts(store, AssetClass.EQUITY, DataType.TRADE)
    ] == [2]
    assert sorted(store.objects) == [
        "live/paper/date=2024-01-02/asset_class=equity/data_type=quote/"
        "SPY.part-000000.parquet",
        "live/paper/date=2024-01-02/asset_class=equity/data_type=trade/"
        "SPY.part-000000.parquet",
    ]

    recorder.record(_trade("t3", offset=4, sequence=5))
    recorder.close()
    assert (
        "live/paper/date=2024-01-02/asset_class=equity/data_type=trade/"
        "SPY.part-000001.parquet"
    ) in store.objects
    assert (
        sum(
            table.num_rows
            for table in _read_parts(store, AssetClass.EQUITY, DataType.TRADE)
        )
        == 3
    )


def test_close_flushes_pending_rows_and_is_idempotent() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=100))
    for index in range(3):
        recorder.record(_quote(f"q{index}", offset=index, sequence=index))
    assert recorder.pending_rows == 3
    assert store.objects == {}

    recorder.close()
    assert recorder.closed
    assert (
        sum(
            table.num_rows
            for table in _read_parts(store, AssetClass.EQUITY, DataType.QUOTE)
        )
        == 3
    )
    recorder.close()
    assert len(store.writes) == 1
    with pytest.raises(RuntimeError, match="closed"):
        recorder.record(_quote("late", offset=9, sequence=9))
    with pytest.raises(RuntimeError, match="closed"):
        recorder.flush()


def test_public_flush_writes_pending_without_closing() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=100))
    recorder.record(_quote("q1", offset=0, sequence=1))

    recorder.flush()
    assert (
        sum(
            table.num_rows
            for table in _read_parts(store, AssetClass.EQUITY, DataType.QUOTE)
        )
        == 1
    )
    assert recorder.pending_rows == 0
    assert not recorder.closed

    recorder.record(_quote("q2", offset=1, sequence=2))
    assert recorder.pending_rows == 1
    recorder.close()
    assert (
        sum(
            table.num_rows
            for table in _read_parts(store, AssetClass.EQUITY, DataType.QUOTE)
        )
        == 2
    )


def test_rotation_finalizes_previous_session_without_close() -> None:
    store = FakeStore()
    recorder = LiveRecorder(
        _config(store, asset_class=AssetClass.CRYPTO, batch_size=100)
    )
    recorder.record(
        TradeEvent(
            "day-one",
            datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
            1,
            Symbol("BTC/USD"),
            Decimal("42000"),
            Decimal("0.25"),
            None,
            None,
        )
    )
    recorder.record(
        TradeEvent(
            "day-two",
            datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
            2,
            Symbol("BTC/USD"),
            Decimal("42001"),
            Decimal("0.25"),
            None,
            None,
        )
    )

    day_one_path = (
        "live/paper/date=2024-01-01/asset_class=crypto/data_type=trade/"
        "BTC%2FUSD.part-000000.parquet"
    )
    assert store.objects[day_one_path]
    recorder.close()
    assert (
        sum(
            table.num_rows
            for table in _read_parts(store, AssetClass.CRYPTO, DataType.TRADE)
        )
        == 2
    )


def test_flush_interval_uses_injected_clock() -> None:
    store = FakeStore()
    clock = SimClock(STAMP)
    recorder = LiveRecorder(
        _config(
            store,
            asset_class=AssetClass.CRYPTO,
            batch_size=100,
            flush_interval=timedelta(minutes=5),
            clock=clock,
        )
    )
    recorder.record(
        TradeEvent(
            "btc",
            STAMP,
            1,
            Symbol("BTC/USD"),
            Decimal("42000"),
            Decimal("0.5"),
            None,
            None,
        )
    )
    clock.advance_to(STAMP + timedelta(minutes=4))
    recorder.record(
        TradeEvent(
            "eth",
            clock.now(),
            2,
            Symbol("ETH/USD"),
            Decimal("3000"),
            Decimal("0.5"),
            None,
            None,
        )
    )
    assert store.objects == {}
    assert recorder.pending_rows == 2

    clock.advance_to(STAMP + timedelta(minutes=6))
    recorder.record(
        TradeEvent(
            "eth-two",
            clock.now(),
            3,
            Symbol("ETH/USD"),
            Decimal("3001"),
            Decimal("0.5"),
            None,
            None,
        )
    )
    assert sorted(store.objects) == [
        "live/paper/date=2024-01-02/asset_class=crypto/data_type=trade/"
        "BTC%2FUSD.part-000000.parquet"
    ]
    assert recorder.pending_rows == 2


def test_local_store_writes_readable_parts_below_deployment_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "live-root"
    recorder = LiveRecorder(_config(str(root), deployment="trader-1", batch_size=1))
    recorder.record(_quote("q1", offset=0, sequence=1))
    recorder.record(_trade("t1", offset=1, sequence=2))
    recorder.close()

    relative_paths = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*.parquet")
    )
    assert relative_paths == [
        "live/trader-1/date=2024-01-02/asset_class=equity/data_type=quote/"
        "SPY.part-000000.parquet",
        "live/trader-1/date=2024-01-02/asset_class=equity/data_type=trade/"
        "SPY.part-000000.parquet",
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        table = pq.read_table(root / relative_paths[0])
    assert table.schema.equals(
        schema_for(AssetClass.EQUITY, DataType.QUOTE), check_metadata=True
    )
    assert table.column("symbol").to_pylist() == ["SPY"]


def test_live_part_path_extends_the_lake_partition_contract() -> None:
    assert live_part_path(
        deployment="trader-1",
        session_date=date(2024, 1, 2),
        asset_class="equity",
        data_type=DataType.TRADE,
        symbol="SPY",
        part=0,
    ) == (
        "live/trader-1/date=2024-01-02/asset_class=equity/data_type=trade/"
        "SPY.part-000000.parquet"
    )
    assert live_part_path(
        deployment="trader-1",
        session_date=date(2024, 1, 2),
        asset_class=AssetClass.CRYPTO,
        data_type=DataType.TRADE,
        symbol="BTC/USD",
        part=1,
    ) == (
        "live/trader-1/date=2024-01-02/asset_class=crypto/data_type=trade/"
        "BTC%2FUSD.part-000001.parquet"
    )
    with pytest.raises(TypeError, match="date, not a datetime"):
        live_part_path(
            deployment="trader-1",
            session_date=STAMP,
            asset_class="equity",
            data_type="trade",
            symbol="SPY",
            part=0,
        )
    with pytest.raises(ValueError, match="deployment"):
        live_part_path(
            deployment="../evil",
            session_date=date(2024, 1, 2),
            asset_class="equity",
            data_type="trade",
            symbol="SPY",
            part=0,
        )
    with pytest.raises(ValueError, match="part"):
        live_part_path(
            deployment="trader-1",
            session_date=date(2024, 1, 2),
            asset_class="equity",
            data_type="trade",
            symbol="SPY",
            part=-1,
        )


def test_equity_rows_match_lake_columns_and_values() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=100))
    recorder.record(_quote("q1", offset=0, sequence=1))
    recorder.record(_trade("t1", offset=1, sequence=2))
    recorder.close()

    quote = _read_parts(store, AssetClass.EQUITY, DataType.QUOTE)[0].to_pylist()[0]
    assert quote == {
        "timestamp": STAMP,
        "symbol": "SPY",
        "bid_exchange": "XNAS",
        "bid_price": Decimal("100.00"),
        "bid_size": 1,
        "ask_exchange": "XNAS",
        "ask_price": Decimal("100.02"),
        "ask_size": 2,
        "conditions": [],
        "tape": "",
    }
    trade = _read_parts(store, AssetClass.EQUITY, DataType.TRADE)[0].to_pylist()[0]
    assert trade == {
        "timestamp": STAMP + timedelta(microseconds=1),
        "symbol": "SPY",
        "trade_id": 2,
        "exchange": "XNAS",
        "price": Decimal("100.01"),
        "size": 2,
        "conditions": [],
        "tape": "",
        "update": None,
    }


def test_crypto_rows_keep_nullable_fields_and_exact_sizes() -> None:
    store = FakeStore()
    recorder = LiveRecorder(
        _config(store, asset_class=AssetClass.CRYPTO, batch_size=100)
    )
    recorder.record(
        QuoteEvent(
            "cq",
            STAMP,
            1,
            Symbol("BTC/USD"),
            Decimal("42000.10"),
            Decimal("0.001"),
            Decimal("42000.90"),
            Decimal("0.002"),
            None,
        )
    )
    recorder.record(
        TradeEvent(
            "ct",
            STAMP + timedelta(microseconds=1),
            2,
            Symbol("BTC/USD"),
            Decimal("42000.50"),
            Decimal("0.25"),
            "CBSE",
            "77",
        )
    )
    recorder.close()

    quote = _read_parts(store, AssetClass.CRYPTO, DataType.QUOTE)[0].to_pylist()[0]
    assert quote["bid_exchange"] is None
    assert quote["ask_exchange"] is None
    assert quote["bid_size"] == Decimal("0.001")
    assert quote["ask_size"] == Decimal("0.002")
    trade = _read_parts(store, AssetClass.CRYPTO, DataType.TRADE)[0].to_pylist()[0]
    assert trade["trade_id"] == 77
    assert trade["exchange"] == "CBSE"
    assert trade["size"] == Decimal("0.25")
    assert trade["taker_side"] is None


def test_equity_trade_requires_integer_identity_and_size() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=100))
    with pytest.raises(ValueError, match="trade_id is required"):
        recorder.record(
            TradeEvent(
                "no-id",
                STAMP,
                1,
                Symbol("SPY"),
                Decimal("100.01"),
                Decimal("2"),
                "XNAS",
                None,
            )
        )
    with pytest.raises(ValueError, match="integer identifier"):
        recorder.record(_trade("bad-id", offset=0, sequence=2, trade_id="abc"))
    with pytest.raises(ValueError, match="integral"):
        recorder.record(
            TradeEvent(
                "fractional",
                STAMP,
                3,
                Symbol("SPY"),
                Decimal("100.01"),
                Decimal("1.5"),
                "XNAS",
                "3",
            )
        )
    assert store.objects == {}

    recorder.record(_trade("ok", offset=0, sequence=4))
    recorder.close()
    assert (
        sum(
            table.num_rows
            for table in _read_parts(store, AssetClass.EQUITY, DataType.TRADE)
        )
        == 1
    )


def test_session_resolver_is_injectable() -> None:
    store = FakeStore()
    recorder = LiveRecorder(
        _config(store, resolver=StaticResolver(date(2024, 3, 3)), batch_size=100)
    )
    recorder.record(_quote("q1", offset=0, sequence=1))
    recorder.record(_quote("q2", offset=1, sequence=2))
    recorder.close()

    assert sorted(store.objects) == [
        "live/paper/date=2024-03-03/asset_class=equity/data_type=quote/"
        "SPY.part-000000.parquet"
    ]


def test_default_resolvers_follow_asset_class() -> None:
    store = FakeStore()
    recorder = LiveRecorder(
        _config(store, asset_class=AssetClass.CRYPTO, batch_size=100)
    )
    recorder.record(
        TradeEvent(
            "before-midnight",
            datetime(2024, 1, 1, 23, 59, tzinfo=UTC),
            1,
            Symbol("ETH/USD"),
            Decimal("3000"),
            Decimal("0.1"),
            None,
            None,
        )
    )
    recorder.record(
        TradeEvent(
            "after-midnight",
            datetime(2024, 1, 2, 0, 1, tzinfo=UTC),
            2,
            Symbol("ETH/USD"),
            Decimal("3001"),
            Decimal("0.1"),
            None,
            None,
        )
    )
    recorder.close()

    assert any("date=2024-01-01" in path for path in store.objects)
    assert any("date=2024-01-02" in path for path in store.objects)


def test_row_counts_and_pending_rows_track_partitions() -> None:
    store = FakeStore()
    recorder = LiveRecorder(_config(store, batch_size=2))
    recorder.record(_quote("q1", offset=0, sequence=1))
    assert recorder.pending_rows == 1
    recorder.record(_quote("q2", offset=1, sequence=2))
    assert recorder.pending_rows == 0
    recorder.record(_trade("t1", offset=2, sequence=3))
    assert recorder.row_counts == {"quote/SPY": 2, "trade/SPY": 0}
    recorder.close()
    assert recorder.row_counts == {"quote/SPY": 2, "trade/SPY": 1}


def test_record_rejects_non_market_events_and_bad_configs(tmp_path: Path) -> None:
    recorder = LiveRecorder(_config(FakeStore()))
    with pytest.raises(TypeError, match="QuoteEvent or TradeEvent"):
        recorder.record(cast("QuoteEvent | TradeEvent", object()))
    with pytest.raises(TypeError, match="LiveRecorderConfig"):
        LiveRecorder(cast("LiveRecorderConfig", object()))
    recorder.close()

    with pytest.raises(ValueError, match="deployment"):
        _config(str(tmp_path), deployment="../evil")
    with pytest.raises(ValueError, match="deployment"):
        _config(str(tmp_path), deployment="")
    with pytest.raises(ValueError, match="batch_size"):
        _config(str(tmp_path), batch_size=0)
    with pytest.raises(ValueError, match="flush_interval"):
        _config(str(tmp_path), flush_interval=timedelta(minutes=-1))
    with pytest.raises(TypeError, match="clock must implement Clock"):
        LiveRecorderConfig(
            deployment="paper",
            asset_class=AssetClass.CRYPTO,
            store=str(tmp_path),
            clock=cast("Clock", object()),
        )
    with pytest.raises(TypeError, match="store must be"):
        LiveRecorderConfig(
            deployment="paper",
            asset_class=AssetClass.CRYPTO,
            store=cast("LiveObjectStore | str", 123),
            clock=SimClock(STAMP),
        )
    with pytest.raises(ValueError, match="AssetClass"):
        LiveRecorderConfig(
            deployment="paper",
            asset_class="nonsense",
            store=str(tmp_path),
            clock=SimClock(STAMP),
        )
    with pytest.raises(TypeError, match="session_resolver"):
        _config(str(tmp_path), resolver=cast("SessionResolver", object()))


def test_local_store_validates_paths_and_replaces_whole_objects(
    tmp_path: Path,
) -> None:
    store = LocalLiveObjectStore(tmp_path / "root")
    store.replace("a/b.part-000000.parquet", b"one")
    store.replace("a/b.part-000000.parquet", b"two")

    assert store.read("a/b.part-000000.parquet") == b"two"
    for bad in ("..\\evil", "/abs", "a/../b", "", "a//b", "a/../../b"):
        with pytest.raises(ValueError):
            store.replace(bad, b"payload")
