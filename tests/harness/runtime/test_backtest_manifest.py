"""Manifest determinism tests for the deterministic backtest runner."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from decimal import Decimal
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
from harness.risk import RiskPipeline
from harness.runtime.backtest import (
    BacktestManifest,
    BacktestOptions,
    BacktestResult,
    BacktestStatus,
    FillModel,
    SyntheticDataOptions,
    build_fill_model,
    build_manifest,
    fee_schedule_from_config,
    preflight_data,
    run_backtest,
    synthetic_fixture,
)
from harness.strategy import Strategy, StrategyContext

STRATEGY_MODULE = "fca_backtest_test_strategies"
BUNDLE = "a" * 64
ONE_MONTH_SESSIONS = 24


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


def _manifest(configuration: BacktestConfig, **kwargs: Any) -> BacktestManifest:
    return _run(configuration, **kwargs).manifest


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_one_month_spy_run_is_artifact_identical_across_repeated_execution() -> None:
    configuration = _configuration()

    first = _run(configuration)
    second = _run(configuration)

    assert first.status is BacktestStatus.COMPLETED
    assert first.artifact_json() == second.artifact_json()
    assert first.artifact_digest() == second.artifact_digest()
    assert first.manifest.manifest_hash == second.manifest.manifest_hash
    assert first.preflight.session_count == ONE_MONTH_SESSIONS
    assert first.market_event_count == first.preflight.event_count
    assert first.market_event_count == 432
    assert first.fills
    assert first.dispatched_count > first.market_event_count
    assert first.portfolio.cash != configuration.initial_cash


def test_manifest_carries_every_reproducibility_input() -> None:
    configuration = _configuration()
    manifest = _manifest(configuration)

    assert manifest.manifest_version == "1"
    assert manifest.engine_id == "harness.backtest"
    assert len(manifest.code_fingerprint) == 64
    assert len(manifest.configuration_hash) == 64
    assert manifest.seed == configuration.seed
    assert manifest.run_id == configuration.run_id
    assert manifest.initial_cash == str(configuration.initial_cash)
    assert manifest.data_fingerprint == configuration.data.fingerprint
    assert manifest.data_snapshot_id == configuration.data.snapshot_id
    assert manifest.data_source_id == "synthetic"
    assert len(manifest.replay_file_fingerprint) == 64
    assert len(manifest.replay_row_fingerprint) == 64
    assert len(manifest.replay_data_fingerprint) == 64
    assert manifest.fill_model == FillModel.SIMPLE_TOUCH.value
    assert len(manifest.fill_model_fingerprint) == 64
    assert manifest.fee_schedule == "harness-config"
    assert len(manifest.fee_fingerprint) == 64
    assert len(manifest.risk_pipeline_fingerprint) == 64
    assert manifest.lookahead_guard is True
    strategy = manifest.strategies[0]
    assert strategy.instance_id == "spy-momentum"
    assert strategy.implementation == f"{STRATEGY_MODULE}:PeriodicMarketStrategy"
    assert strategy.bundle_hash == BUNDLE
    assert strategy.symbols == ("SPY",)
    assert len(strategy.parameter_hash) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        "seed",
        "data_fingerprint",
        "bundle_hash",
        "parameters",
        "fees",
        "risk",
        "fill_model",
        "lookahead_guard",
        "data_source",
        "data_rows",
    ],
)
def test_changing_any_reproducibility_input_changes_the_manifest_hash(
    mutation: str,
) -> None:
    configuration = _configuration()
    baseline = _manifest(configuration).manifest_hash
    options = BacktestOptions()

    if mutation == "seed":
        changed = configuration.model_copy(update={"seed": configuration.seed + 1})
    elif mutation == "data_fingerprint":
        data = configuration.data.model_copy(update={"fingerprint": "c" * 64})
        changed = configuration.model_copy(update={"data": data})
    elif mutation == "bundle_hash":
        instance = _instance(parameters={"quantity": "10", "every": "2"}).model_copy(
            update={"bundle_hash": "d" * 64}
        )
        changed = configuration.model_copy(update={"strategy_instances": (instance,)})
    elif mutation == "parameters":
        instance = _instance(parameters={"quantity": "20", "every": "2"})
        changed = configuration.model_copy(update={"strategy_instances": (instance,)})
    elif mutation == "fees":
        rule = configuration.fees.rules[0].model_copy(update={"rate": Decimal("0.02")})
        fees = configuration.fees.model_copy(update={"rules": (rule,)})
        changed = configuration.model_copy(update={"fees": fees})
    elif mutation == "risk":
        rule = configuration.risk_pipeline.rules[0].model_copy(
            update={"parameters": {"limit": Decimal("1.00")}}
        )
        pipeline = configuration.risk_pipeline.model_copy(update={"rules": (rule,)})
        changed = configuration.model_copy(update={"risk_pipeline": pipeline})
    elif mutation == "fill_model":
        changed = configuration
        options = BacktestOptions(fill_model=FillModel.TRADE_THROUGH)
    elif mutation == "lookahead_guard":
        changed = configuration
        options = BacktestOptions(lookahead_guard=False)
    elif mutation == "data_source":
        changed = configuration
    elif mutation == "data_rows":
        changed = configuration
        options = BacktestOptions(
            synthetic_options=SyntheticDataOptions(quotes_per_session=16)
        )
    else:  # pragma: no cover - parametrize guard
        raise AssertionError(mutation)

    if mutation == "data_source":
        fixture = synthetic_fixture(
            changed, SyntheticDataOptions(quotes_per_session=12, venue="NYSE")
        )
        mutated = _manifest(changed, data_source=fixture)
    else:
        mutated = _manifest(changed, options=options)

    assert mutated.manifest_hash != baseline


def test_manifest_can_be_built_without_running_the_engine() -> None:
    configuration = _configuration()
    fixture = synthetic_fixture(configuration)
    preflight = preflight_data(configuration, fixture)

    manifest = build_manifest(
        configuration,
        fixture=fixture,
        preflight=preflight,
        plugin=build_fill_model(FillModel.SIMPLE_TOUCH),
        schedule=fee_schedule_from_config(configuration.fees),
        pipeline=RiskPipeline(configuration.risk_pipeline),
    )

    assert isinstance(manifest, BacktestManifest)
    assert manifest.manifest_hash == _manifest(configuration).manifest_hash
