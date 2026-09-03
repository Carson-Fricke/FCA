"""Data preflight and local replay fixture tests for the backtest runner."""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from harness.configuration import BacktestConfig, StrategyInstanceConfig, load_example
from harness.core.types import (
    Fill,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.runtime.backtest import (
    BacktestResult,
    DataPreflightError,
    ReplayFixture,
    SyntheticDataOptions,
    backtest_sessions,
    local_lake_fixture,
    preflight_data,
    run_backtest,
    synthetic_fixture,
    write_synthetic_lake,
)
from harness.strategy import Strategy, StrategyContext

STRATEGY_MODULE = "fca_backtest_test_strategies"
BUNDLE = "a" * 64


# --------------------------------------------------------------------------
# Test strategies
# --------------------------------------------------------------------------


class PeriodicMarketStrategy(Strategy):
    """Submit one deterministic order every ``every`` quotes."""

    def __init__(self, **parameters: object) -> None:
        self.quantity = Decimal(str(parameters.get("quantity", "10")))
        self.every = int(str(parameters.get("every", "3")))
        self.order_type = OrderType(str(parameters.get("order_type", "market")))
        self.limit_offset = Decimal(str(parameters.get("limit_offset", "0")))
        self.quotes = 0
        self.submitted: list[str] = []
        self.fills = 0
        self.started = False
        self.stopped = False
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.started = True
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        self.quotes += 1
        if self.quotes % self.every:
            return
        side = Side.BUY if (self.quotes // self.every) % 2 else Side.SELL
        limit = (
            None
            if self.order_type is OrderType.MARKET
            else event.ask_price + self.limit_offset
        )
        order = SingleLegOrder(
            event_id=f"order-{self.quotes}",
            timestamp=event.timestamp,
            sequence=event.sequence,
            order_id=f"order-{self.quotes}",
            symbol=event.symbol,
            side=side,
            quantity=self.quantity,
            order_type=self.order_type,
            time_in_force=TimeInForce.DAY,
            limit_price=limit,
        )
        self.submitted.append(order.order_id)
        self.context.orders.submit(order)

    def on_fill(self, event: Fill) -> None:
        self.fills += 1

    def on_stop(self, context: StrategyContext) -> None:
        self.stopped = True


def _register_strategy_module() -> None:
    """Expose the test strategies under one importable module name."""
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (("PeriodicMarketStrategy", PeriodicMarketStrategy),):
        setattr(module, name, value)
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


# --------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------


def _example() -> BacktestConfig:
    configuration = load_example("backtest")
    assert isinstance(configuration, BacktestConfig)
    return configuration


def _instance(
    instance_id: str = "spy-momentum",
    implementation: str = f"{STRATEGY_MODULE}:PeriodicMarketStrategy",
    *,
    parameters: dict[str, Any] | None = None,
    capital_fraction: Decimal = Decimal("1.0"),
) -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=instance_id,
        implementation=implementation,
        bundle_hash=BUNDLE,
        capital_fraction=capital_fraction,
        symbols=("SPY",),
        parameters={} if parameters is None else parameters,
    )


def _configuration(**overrides: object) -> BacktestConfig:
    updates: dict[str, object] = {
        "run_id": "backtest-determinism",
        "start": datetime(2024, 1, 2, 13, 30, tzinfo=UTC),
        "end": datetime(2024, 2, 2, 20, 0, tzinfo=UTC),
        "strategy_instances": (_instance(parameters={"quantity": "10", "every": "2"}),),
    }
    updates.update(overrides)
    return _example().model_copy(update=updates)


def _run(configuration: BacktestConfig, **kwargs: Any) -> BacktestResult:
    return run_backtest(configuration, **kwargs)


# --------------------------------------------------------------------------
# Data preflight and local fixtures
# --------------------------------------------------------------------------


def test_preflight_rejects_a_fixture_missing_configured_symbols() -> None:
    configuration = _configuration()
    fixture = synthetic_fixture(configuration, SyntheticDataOptions(symbol="AAPL"))

    with pytest.raises(DataPreflightError, match="missing configured symbols"):
        preflight_data(configuration, fixture)


def test_preflight_rejects_events_outside_the_configured_window() -> None:
    configuration = _configuration()
    fixture = synthetic_fixture(configuration)
    clipped = configuration.model_copy(
        update={"end": datetime(2024, 1, 3, 20, 0, tzinfo=UTC)}
    )

    with pytest.raises(DataPreflightError, match="outside the configured run window"):
        preflight_data(clipped, fixture)


def test_preflight_rejects_an_empty_fixture() -> None:
    configuration = _configuration()
    fixture = ReplayFixture(
        sessions=backtest_sessions(configuration),
        source_id="empty",
        symbols=("SPY",),
    )

    with pytest.raises(DataPreflightError, match="at least one event"):
        preflight_data(configuration, fixture)


def test_preflight_rejects_an_unexpected_data_fingerprint() -> None:
    configuration = _configuration()
    fixture = replace(
        synthetic_fixture(configuration), expected_data_fingerprint="e" * 64
    )

    with pytest.raises(DataPreflightError, match="does not match"):
        preflight_data(configuration, fixture)


def test_local_parquet_directory_fixture_replays_the_same_outcome(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    directory = tmp_path / "lake"
    fixture = write_synthetic_lake(configuration, directory)

    assert fixture.source_id == "local-lake"
    assert any(directory.rglob("*.parquet"))

    from_directory = _run(configuration, data_source=directory)
    in_memory = _run(configuration)

    assert from_directory.outcome_digest() == in_memory.outcome_digest()
    assert from_directory.fills == in_memory.fills
    assert from_directory.artifact_digest() != in_memory.artifact_digest()
    assert (
        from_directory.manifest.replay_file_fingerprint
        != in_memory.manifest.replay_file_fingerprint
    )
    assert (
        from_directory.manifest.replay_row_fingerprint
        == in_memory.manifest.replay_row_fingerprint
    )
    assert from_directory.preflight.file_count == fixture.file_count


def test_local_lake_fixture_rejects_a_missing_directory(tmp_path: Path) -> None:
    configuration = _configuration()

    with pytest.raises(DataPreflightError, match="does not exist"):
        local_lake_fixture(configuration, tmp_path / "absent")
