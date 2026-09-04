"""Determinism data-path scenarios: history reads and fabricated events.

The determinism harness hands strategies the opaque, now-anchored
:class:`~harness.data.history.StrategyHistory` capability when a scenario
enables history, so the determinism oracle arbitrates the strategy data path
end to end. These scenarios pin that every read is clipped to the engine
clock head, that the capability hides the engine view's mutating surfaces,
and that the scheduler still refuses fabricated market data with the
established error taxonomy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent, TradeEvent
from harness.data import HistoricalView, StrategyHistory
from harness.strategy import Subscription
from harness.testing.determinism import (
    DataProbeStrategy,
    DeterminismScenario,
    FabricatedQuoteStrategy,
    SyntheticFeed,
    ToyStrategy,
    assert_reproducible,
    compare_manifests,
    run_scenario,
)
from harness.venues import ScriptedStep, VenueCall, VenueOperation

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def quote(event_id: str, offset: int = 0) -> QuoteEvent:
    return QuoteEvent(
        event_id=event_id,
        timestamp=STAMP + timedelta(microseconds=offset),
        sequence=offset,
        symbol=Symbol("AAPL"),
        bid_price=Decimal("100"),
        bid_quantity=Decimal("10"),
        ask_price=Decimal("101"),
        ask_quantity=Decimal("10"),
        venue="determinism",
    )


def trade(event_id: str) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        timestamp=STAMP + timedelta(microseconds=500),
        sequence=1,
        symbol=Symbol("AAPL"),
        price=Decimal("100.5"),
        quantity=Decimal("3"),
        venue="determinism",
    )


def data_scenario() -> DeterminismScenario:
    return DeterminismScenario(
        feed=SyntheticFeed(
            (quote("data-quote-1"), trade("data-trade-1"), quote("data-quote-2", 1000))
        ),
        venue_steps=(),
        strategy_class=DataProbeStrategy,
        configuration={"symbols": ["AAPL"], "mode": "determinism"},
        history=True,
        subscriptions=(Subscription(QuoteEvent), Subscription(TradeEvent)),
        start=STAMP,
    )


def fabrication_scenario() -> DeterminismScenario:
    return DeterminismScenario(
        feed=SyntheticFeed((quote("fabrication-quote"),)),
        venue_steps=(),
        strategy_class=FabricatedQuoteStrategy,
        configuration={"symbols": ["AAPL"], "mode": "determinism"},
        start=STAMP,
    )


def toy_scenario(*, history: bool) -> DeterminismScenario:
    return DeterminismScenario(
        feed=SyntheticFeed((quote("toy-quote"),)),
        venue_steps=(
            ScriptedStep(
                VenueCall(
                    VenueOperation.SUBMIT,
                    order_id="toy-order",
                ),
            ),
        ),
        strategy_class=ToyStrategy,
        strategy_parameters={"order_id": "toy-order"},
        configuration={"symbols": ["AAPL"], "mode": "determinism"},
        history=history,
        start=STAMP,
    )


def test_data_scenario_is_reproducible_and_reads_stay_now_anchored() -> None:
    first, second = assert_reproducible(data_scenario())

    assert first.dispatched_count == 3
    assert first.final_clock == STAMP + timedelta(microseconds=1000)
    names = sorted(artifact.name for artifact in first.artifacts)
    assert names == [
        "dispatch/harness.core.types.events.QuoteEvent.parquet",
        "dispatch/harness.core.types.events.TradeEvent.parquet",
        "feed/harness.core.types.events.QuoteEvent.parquet",
        "feed/harness.core.types.events.TradeEvent.parquet",
        "portfolio/snapshot.json",
        "strategy/output.json",
        "venue/calls.json",
    ]
    assert json.loads(first.artifact("venue/calls.json").payload) == []
    assert first.dispatch_hash == second.dispatch_hash

    for run in (first, second):
        probe = run.strategy
        assert isinstance(probe, DataProbeStrategy)
        assert probe.observations == []
        assert probe.callbacks == [
            "on_start",
            "on_quote",
            "on_trade",
            "on_quote",
            "on_stop",
        ]
        context = run.binding.context
        assert isinstance(context.data, StrategyHistory)
        assert not isinstance(context.data, HistoricalView)
        output = json.loads(run.artifact("strategy/output.json").payload)
        surfaces = [
            record["fields"]["surface_type"]
            for record in output["logs"]
            if record["message"] == "data-probe"
        ]
        assert surfaces == ["harness.data.history.StrategyHistory"] * 5
        reads = [
            record["fields"]
            for record in output["logs"]
            if record["message"] == "data-read"
        ]
        assert [read["phase"] for read in reads] == [
            "on_quote",
            "on_trade",
            "on_quote",
            "on_stop",
        ]
        assert reads[0] == {
            "phase": "on_quote",
            "symbol": "AAPL",
            "latest": "data-quote-1",
            "latest_quote": "data-quote-1",
            "latest_trade": None,
            "recent": ["data-quote-1"],
            "window": [],
            "quote_count": 0,
            "trade_count": 0,
            "volume": {"$decimal": "0"},
            "vwap": None,
        }
        assert reads[1] == {
            "phase": "on_trade",
            "symbol": "AAPL",
            "latest": "data-trade-1",
            "latest_quote": "data-quote-1",
            "latest_trade": "data-trade-1",
            "recent": ["data-quote-1", "data-trade-1"],
            "window": ["data-quote-1"],
            "quote_count": 1,
            "trade_count": 0,
            "volume": {"$decimal": "0"},
            "vwap": None,
        }
        assert reads[2] == {
            "phase": "on_quote",
            "symbol": "AAPL",
            "latest": "data-quote-2",
            "latest_quote": "data-quote-2",
            "latest_trade": "data-trade-1",
            "recent": ["data-trade-1", "data-quote-2"],
            "window": ["data-trade-1"],
            "quote_count": 0,
            "trade_count": 1,
            "volume": {"$decimal": "3"},
            "vwap": {"$decimal": "100.5"},
        }
        assert reads[3] == {**reads[2], "phase": "on_stop"}


def test_history_flag_only_moves_configuration_identity() -> None:
    plain_first, _ = assert_reproducible(toy_scenario(history=False))
    history_first, _ = assert_reproducible(toy_scenario(history=True))

    assert plain_first.binding.context.data is None
    assert isinstance(history_first.binding.context.data, StrategyHistory)
    assert plain_first.artifacts == history_first.artifacts
    assert compare_manifests(plain_first, history_first).differences == (
        "configuration_hash",
    )


def test_fabricated_event_refusal_is_deterministic_and_clean() -> None:
    messages = []
    for _ in range(2):
        with pytest.raises(TypeError) as excinfo:
            run_scenario(fabrication_scenario())
        messages.append(str(excinfo.value))

    assert messages[0] == messages[1]
    assert messages[0] == (
        "QuoteEvent events are reserved for the feed and the venue; "
        "strategies cannot schedule them"
    )
