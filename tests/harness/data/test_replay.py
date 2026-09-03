from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import ParquetCodec, QuoteEvent, TradeEvent
from harness.data.replay import LakeReplayFeed
from lake.contracts import ExchangeSession
from lakeclient import AssetClass, DataType, FileQueryFile

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
ONE = Decimal("1")


def _session(day: date = date(2024, 1, 19)) -> ExchangeSession:
    start = datetime.combine(day, datetime.min.time(), UTC)
    return ExchangeSession(day, start, start + timedelta(days=1))


def _quote(
    event_id: str, *, offset: int, sequence: int, symbol: str = "SPY"
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol(symbol),
        Decimal("100"),
        ONE,
        Decimal("101"),
        ONE,
    )


def _trade(
    event_id: str, *, offset: int, sequence: int, symbol: str = "SPY"
) -> TradeEvent:
    return TradeEvent(
        event_id,
        STAMP + timedelta(microseconds=offset),
        sequence,
        Symbol(symbol),
        Decimal("100.5"),
        ONE,
    )


def test_same_timestamp_merge_uses_sequence_class_priority_then_feed_order() -> None:
    feed = LakeReplayFeed(
        (_session(),),
        streams={
            ("SPY", "trade"): (
                _trade("trade-sequence", offset=0, sequence=2),
                _trade("trade-tie", offset=0, sequence=4),
            ),
            ("SPY", "quote"): (
                _quote("quote-sequence", offset=0, sequence=2),
                _quote("quote-first", offset=0, sequence=4),
                _quote("quote-second", offset=0, sequence=4),
            ),
        },
    )

    assert [event.event_id for event in feed] == [
        "quote-sequence",
        "trade-sequence",
        "quote-first",
        "quote-second",
        "trade-tie",
    ]


def test_stream_can_publish_to_event_bus_without_importing_a_facade() -> None:
    feed = LakeReplayFeed(
        (_session(),),
        streams={"SPY": (_quote("q", offset=0, sequence=1),)},
    )
    bus = EventBus(SimClock(STAMP))

    assert feed.publish(bus) == 1
    assert [event.event_id for event in bus.drain()] == ["q"]


def test_repeated_loads_have_stable_file_row_and_data_fingerprints() -> None:
    events = (
        _quote("q", offset=0, sequence=1),
        _trade("t", offset=1, sequence=2),
    )
    first = LakeReplayFeed((_session(),), streams={"SPY": events})
    second = LakeReplayFeed((_session(),), streams={"SPY": events})

    assert tuple(first)
    assert tuple(second)
    assert first.fingerprints == second.fingerprints
    assert len(first.file_fingerprint) == 64
    assert len(first.row_fingerprint) == 64
    assert len(first.data_fingerprint) == 64


def test_parquet_reader_is_opened_per_session_and_receives_codec_bytes() -> None:
    first_day = date(2024, 1, 19)
    second_day = date(2024, 1, 20)
    sessions = (_session(first_day), _session(second_day))
    first_event = _quote("first", offset=0, sequence=1)
    second_event = QuoteEvent(
        "second",
        datetime.combine(second_day, datetime.min.time(), UTC),
        1,
        Symbol("SPY"),
        Decimal("100"),
        ONE,
        Decimal("101"),
        ONE,
    )
    payloads = {
        first_day: ParquetCodec.encode((first_event,)),
        second_day: ParquetCodec.encode((second_event,)),
    }
    calls: list[date] = []

    files = tuple(
        FileQueryFile(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.QUOTE,
            symbol="SPY",
            session_date=day,
            object_path=(f"date={day}/asset_class=equity/data_type=quote/SPY.parquet"),
            url="unused",
            sas_url="unused",
            row_count=1,
            byte_size=len(payloads[day]),
            checksum="checksum",
        )
        for day in (first_day, second_day)
    )

    def reader(file: FileQueryFile) -> bytes:
        day = file.session_date
        calls.append(day)
        return payloads[day]

    feed = LakeReplayFeed(sessions, files=files, reader=reader)

    assert [event.event_id for event in feed] == ["first", "second"]
    assert calls == [first_day, second_day]


def test_large_lazy_stream_does_not_allocate_or_consume_a_year_up_front() -> None:
    consumed = 0
    year_start = date(2024, 1, 2)
    sessions = tuple(
        _session(year_start + timedelta(days=index)) for index in range(252)
    )

    def one_year_of_quotes(symbol: str) -> Iterator[QuoteEvent]:
        nonlocal consumed
        for index in range(252):
            consumed += 1
            day = year_start + timedelta(days=index)
            timestamp = datetime.combine(day, datetime.min.time(), UTC)
            yield QuoteEvent(
                f"{symbol}-{index}",
                timestamp,
                index,
                Symbol(symbol),
                Decimal("100"),
                ONE,
                Decimal("101"),
                ONE,
            )

    def one_year_of_trades(symbol: str) -> Iterator[TradeEvent]:
        nonlocal consumed
        for index in range(252):
            consumed += 1
            day = year_start + timedelta(days=index)
            timestamp = datetime.combine(day, datetime.min.time(), UTC)
            yield TradeEvent(
                f"{symbol}-trade-{index}",
                timestamp,
                index,
                Symbol(symbol),
                Decimal("100.5"),
                ONE,
            )

    feed = LakeReplayFeed(
        sessions,
        quote_streams={
            "SPY": lambda: one_year_of_quotes("SPY"),
            "QQQ": lambda: one_year_of_quotes("QQQ"),
        },
        trade_streams={
            "SPY": lambda: one_year_of_trades("SPY"),
            "QQQ": lambda: one_year_of_trades("QQQ"),
        },
    )

    iterator = iter(feed)
    assert next(iterator).event_id == "SPY-0"
    assert consumed == 4


def test_source_order_must_be_monotonic_and_symbols_must_match() -> None:
    with pytest.raises(ValueError, match="ordered"):
        tuple(
            LakeReplayFeed(
                (_session(),),
                streams={
                    "SPY": (
                        _quote("late", offset=1, sequence=1),
                        _quote("early", offset=0, sequence=2),
                    )
                },
            )
        )

    with pytest.raises(ValueError, match="does not match"):
        tuple(
            LakeReplayFeed(
                (_session(),),
                streams={"SPY": (_quote("wrong", offset=0, sequence=1, symbol="QQQ"),)},
            )
        )
