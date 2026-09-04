"""Golden boundary scenarios: venue-state blindness and schedule() limits.

The golden harness hands strategies a one-way venue command capability and a
scheduler that refuses fabricated market data. These scenarios exercise both
boundaries with deterministic, byte-identical artifacts, and pin the
valid-strategy artifact surface so capability narrowing stays
semantics-preserving.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent
from harness.runtime.backtest.strategies import StrategyVenue
from harness.strategy import Strategy, StrategyContext
from harness.testing.golden import (
    GoldenScenario,
    OrderSignalStrategy,
    SyntheticFeed,
    ToyStrategy,
    VenueStateProbeStrategy,
    assert_reproducible,
    run_scenario,
)
from harness.venues import ScriptedStep, ScriptedVenue, VenueCall, VenueOperation

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def quote(event_id: str) -> QuoteEvent:
    return QuoteEvent(
        event_id=event_id,
        timestamp=STAMP,
        sequence=0,
        symbol=Symbol("AAPL"),
        bid_price=Decimal("100"),
        bid_quantity=Decimal("10"),
        ask_price=Decimal("101"),
        ask_quantity=Decimal("10"),
        venue="golden",
    )


def probe_scenario() -> GoldenScenario:
    return GoldenScenario(
        feed=SyntheticFeed((quote("probe-quote"),)),
        venue_steps=(),
        strategy_class=VenueStateProbeStrategy,
        configuration={"symbols": ["AAPL"], "mode": "golden"},
        start=STAMP,
    )


def signal_scenario() -> GoldenScenario:
    return GoldenScenario(
        feed=SyntheticFeed((quote("signal-quote"),)),
        venue_steps=(),
        strategy_class=OrderSignalStrategy,
        strategy_parameters={"order_id": "signal-order"},
        configuration={"symbols": ["AAPL"], "mode": "golden"},
        start=STAMP,
    )


def test_probe_scenario_is_reproducible_and_observes_no_venue_state() -> None:
    first, second = assert_reproducible(probe_scenario())

    for run in (first, second):
        probe = run.strategy
        assert isinstance(probe, VenueStateProbeStrategy)
        assert probe.observations == []
        context = run.binding.context
        assert isinstance(context.venue, StrategyVenue)
        assert not isinstance(context.venue, ScriptedVenue)
        assert context.venue is context.orders.venue
        output = json.loads(run.artifact("strategy/output.json").payload)
        observed = [
            record
            for record in output["logs"]
            if record["message"] == "venue-state-observed"
        ]
        assert observed == []
        quote_surfaces = [
            record["fields"]["surface_type"]
            for record in output["logs"]
            if record["message"] == "venue-probe"
            and record["fields"]["phase"] == "on_quote"
        ]
        assert quote_surfaces == [
            "harness.runtime.backtest.strategies.StrategyVenue",
            "harness.strategy.context.OrdersView",
            "harness.runtime.backtest.strategies.StrategyVenue",
        ]
        phases = {
            record["fields"]["phase"]
            for record in output["logs"]
            if record["message"] == "venue-probe"
        }
        assert phases == {"on_start", "on_quote", "on_stop"}


def test_signal_scenario_is_reproducible_and_fabricates_no_market_data() -> None:
    first, second = assert_reproducible(signal_scenario())

    for run in (first, second):
        assert run.dispatched_count == 2
        strategy = run.strategy
        assert isinstance(strategy, OrderSignalStrategy)
        assert [order.order_id for order in strategy.scheduled] == ["signal-order"]
        assert run.artifact(
            "dispatch/harness.core.types.events.QuoteEvent.parquet"
        ).payload.startswith(b"PAR1")
        scheduled = run.artifact(
            "dispatch/harness.core.types.orders.SingleLegOrder.parquet"
        )
        assert scheduled.payload.startswith(b"PAR1")
        assert json.loads(run.artifact("venue/calls.json").payload) == []
    assert first.dispatch_hash == second.dispatch_hash


class FabricatingQuoteStrategy(Strategy):
    """Attempt to schedule a fabricated quote; the scheduler must refuse it."""

    def __init__(self) -> None:
        self.callbacks: list[str] = []
        self._context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self._context = context

    def on_quote(self, event: QuoteEvent) -> None:
        self.callbacks.append("on_quote")
        context = self._context
        assert context is not None
        context.schedule(
            QuoteEvent(
                event_id="fabricated-quote",
                timestamp=event.timestamp,
                sequence=event.sequence + 1,
                symbol=event.symbol,
                bid_price=event.bid_price,
                bid_quantity=event.bid_quantity,
                ask_price=event.ask_price,
                ask_quantity=event.ask_quantity,
                venue="fabricated",
            )
        )


def test_strategy_scheduler_refuses_fabricated_market_data() -> None:
    scenario = GoldenScenario(
        feed=SyntheticFeed((quote("fabrication-quote"),)),
        venue_steps=(),
        strategy_class=FabricatingQuoteStrategy,
        configuration={"symbols": ["AAPL"], "mode": "golden"},
        start=STAMP,
    )

    with pytest.raises(TypeError, match="reserved"):
        run_scenario(scenario)


def test_capability_boundary_leaves_valid_strategy_artifacts_unchanged() -> None:
    scenario = GoldenScenario(
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
        strategy_parameters={"order_id": "toy-order", "quantity": Decimal("2")},
        configuration={"symbols": ["AAPL"], "mode": "golden"},
        start=STAMP,
    )
    first, second = assert_reproducible(scenario)

    names = sorted(artifact.name for artifact in first.artifacts)
    assert names == [
        "dispatch/harness.core.types.events.QuoteEvent.parquet",
        "feed/harness.core.types.events.QuoteEvent.parquet",
        "portfolio/snapshot.json",
        "strategy/orders.parquet",
        "strategy/output.json",
        "venue/calls.json",
    ]
    assert json.loads(first.artifact("venue/calls.json").payload) == [
        {
            "account_id": None,
            "operation": "submit",
            "order_id": "toy-order",
            "replacement_order_id": None,
        }
    ]
    output = json.loads(first.artifact("strategy/output.json").payload)
    assert output["submitted_order_ids"] == ["toy-order"]
    assert first.dispatched_count == 1
    assert first.dispatch_hash == second.dispatch_hash
