"""Cross-mode indicator parity: streamed, kernel-bus, and snapshot-resumed
delivery of one canonical tick sequence must produce identical outputs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types import Event, QuoteEvent, TradeEvent
from harness.indicators import MIDNIGHT_UTC
from harness.testing.indicator_parity import (
    IndicatorParityConfigurationError,
    IndicatorParityScenario,
    IndicatorRequest,
    assert_indicator_parity,
    run_indicator_parity,
)

DAY_ONE = datetime(2024, 1, 19, 23, 58, 30, tzinfo=UTC)


def _trade(
    moment: datetime, sequence: int, price: str, quantity: str, symbol: str = "AAPL"
) -> TradeEvent:
    return TradeEvent(
        f"t{sequence}",
        moment,
        sequence,
        Symbol(symbol),
        Decimal(price),
        Decimal(quantity),
    )


def _quote(
    moment: datetime, sequence: int, bid: str, ask: str, symbol: str = "AAPL"
) -> QuoteEvent:
    return QuoteEvent(
        f"q{sequence}",
        moment,
        sequence,
        Symbol(symbol),
        Decimal(bid),
        Decimal("1"),
        Decimal(ask),
        Decimal("1"),
    )


def _canonical_events() -> tuple[QuoteEvent | TradeEvent, ...]:
    second = timedelta(seconds=1)
    minute = timedelta(minutes=1)
    day_two = datetime(2024, 1, 20, tzinfo=UTC)
    return (
        _trade(DAY_ONE, 1, "100.00", "2"),
        _quote(DAY_ONE + 40 * second, 2, "99.90", "100.10"),
        _trade(DAY_ONE + minute + 10 * second, 3, "101.00", "1"),
        _trade(DAY_ONE + minute + 20 * second, 4, "200.00", "3", "MSFT"),
        _trade(day_two + 30 * second, 5, "102.00", "1"),
        _quote(day_two + 40 * second, 6, "101.90", "102.10"),
        _trade(day_two + minute, 7, "101.50", "2"),
        _trade(day_two + minute + 30 * second, 8, "201.00", "1", "MSFT"),
        _quote(day_two + 2 * minute + 10 * second, 9, "100.40", "100.60"),
        _trade(day_two + 2 * minute + 10 * second, 9, "100.50", "1"),
        _quote(day_two + 2 * minute + 20 * second, 11, "200.80", "201.20", "MSFT"),
        _trade(day_two + 3 * minute, 12, "101.20", "1"),
    )


def _requests() -> tuple[IndicatorRequest, ...]:
    return (
        IndicatorRequest("sma", "AAPL", {"period": 2}),
        IndicatorRequest("ema", "AAPL", {"span": 3}),
        IndicatorRequest("rsi", "AAPL", {"period": 4}),
        IndicatorRequest("atr", "AAPL", {"period": 4}),
        IndicatorRequest("realized_vol", "AAPL", {"window": "5min"}),
        IndicatorRequest("dollar_volume", "AAPL", {"window": "1min"}),
        IndicatorRequest("dollar_volume", "AAPL"),
        IndicatorRequest("vwap", "AAPL"),
        IndicatorRequest("obv", "AAPL"),
        IndicatorRequest("mid_sma", "AAPL", {"period": 2}),
        IndicatorRequest("vwap", "MSFT"),
        IndicatorRequest("sma", "MSFT", {"period": 2}),
    )


def _scenario() -> IndicatorParityScenario:
    return IndicatorParityScenario(
        _canonical_events(),
        _requests(),
        jitter=4,
        resume_every=1,
        session_anchor=None,
    )


def test_cross_mode_parity_is_clean() -> None:
    report = run_indicator_parity(_scenario())
    assert report.sample_count > 0
    assert report.parity_clean, report.summary()
    assert report.dimensions[0].name == "mode:bus_replay"
    assert report.dimensions[1].name == "mode:snapshot_resume"
    assert report.dimensions[2].name == "snapshot_bytes"
    assert report.dimensions[3].name == "mode:determinism_rerun"


def test_assert_helper_accepts_a_clean_scenario() -> None:
    assert_indicator_parity(_scenario())


def test_parity_holds_with_larger_jitter_and_resume_cadence() -> None:
    scenario = IndicatorParityScenario(
        _canonical_events(),
        _requests(),
        jitter=5,
        resume_every=3,
        session_anchor=None,
    )
    report = run_indicator_parity(scenario)
    assert report.parity_clean, report.summary()


def test_parity_holds_across_a_midnight_session_boundary() -> None:
    scenario = IndicatorParityScenario(
        _canonical_events(),
        _requests(),
        session_anchor=MIDNIGHT_UTC,
    )
    report = run_indicator_parity(scenario)
    assert report.parity_clean, report.summary()


def test_parity_runs_are_deterministic_across_runs() -> None:
    first = run_indicator_parity(_scenario())
    second = run_indicator_parity(_scenario())
    assert first.scenario_hash == second.scenario_hash
    assert first == second


def test_scenario_hash_pins_every_input() -> None:
    base = _scenario()
    assert base.scenario_hash == _scenario().scenario_hash
    jittered = IndicatorParityScenario(
        _canonical_events(),
        _requests(),
        jitter=3,
        session_anchor=None,
    )
    assert jittered.scenario_hash != base.scenario_hash


def test_scenario_rejects_events_out_of_kernel_order() -> None:
    events = list(_canonical_events())
    events[1], events[2] = events[2], events[1]
    with pytest.raises(
        IndicatorParityConfigurationError, match="kernel dispatch order"
    ):
        IndicatorParityScenario(events, _requests(), session_anchor=None)


def test_scenario_rejects_malformed_inputs() -> None:
    with pytest.raises(IndicatorParityConfigurationError, match="QuoteEvent"):
        IndicatorParityScenario(
            (Event("order-1", DAY_ONE, 1),),  # ty: ignore[invalid-argument-type]
            _requests(),
            session_anchor=None,
        )
    with pytest.raises(IndicatorParityConfigurationError, match="empty"):
        IndicatorParityScenario((), _requests(), session_anchor=None)
    with pytest.raises(IndicatorParityConfigurationError, match="jitter"):
        IndicatorParityScenario(
            _canonical_events(),
            _requests(),
            jitter=0,
            session_anchor=None,
        )


def test_canonical_stream_covers_quotes_trades_ties_and_midnight() -> None:
    events = _canonical_events()
    assert any(isinstance(event, QuoteEvent) for event in events)
    assert any(
        event.timestamp.date() == datetime(2024, 1, 20, tzinfo=UTC).date()
        for event in events
    )
    tied = [event for event in events if event.timestamp == events[8].timestamp]
    assert len(tied) == 2
    assert isinstance(tied[0], QuoteEvent)
    assert isinstance(tied[1], TradeEvent)
    assert tied[0].sequence == tied[1].sequence
