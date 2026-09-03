"""Runner behavior tests: policies, risk, fills, fees, and loading errors."""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from harness.configuration import BacktestConfig, StrategyInstanceConfig, load_example
from harness.core.types import (
    Fill,
    OrderRequest,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.risk import RiskContext, RuleDecision
from harness.runtime.backtest import (
    AbortPolicy,
    BacktestAbort,
    BacktestAborted,
    BacktestConfigurationError,
    BacktestOptions,
    BacktestResult,
    BacktestStatus,
    FillModel,
    LookaheadViolationError,
    StrategyExecutionError,
    StrategyHalt,
    StrategyHaltPolicy,
    StrategyLoadError,
    TeardownPolicy,
    run_backtest,
)
from harness.strategy import Strategy, StrategyContext

STRATEGY_MODULE = "fca_backtest_test_strategies"
BUNDLE = "a" * 64
ZERO = Decimal("0")


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


class HaltingStrategy(Strategy):
    """Raise :class:`StrategyHalt` after a deterministic number of quotes."""

    def __init__(self, **parameters: object) -> None:
        self.halt_at = int(str(parameters.get("halt_at", "2")))
        self.quotes = 0
        self.stopped = False

    def on_quote(self, event: QuoteEvent) -> None:
        self.quotes += 1
        if self.quotes >= self.halt_at:
            raise StrategyHalt(f"halt after {self.quotes} quotes")

    def on_stop(self, context: StrategyContext) -> None:
        self.stopped = True


class AbortingStrategy(Strategy):
    """Raise :class:`BacktestAbort` after a deterministic number of quotes."""

    def __init__(self, **parameters: object) -> None:
        self.abort_at = int(str(parameters.get("abort_at", "3")))
        self.quotes = 0
        self.stopped = False

    def on_quote(self, event: QuoteEvent) -> None:
        self.quotes += 1
        if self.quotes >= self.abort_at:
            raise BacktestAbort(f"abort after {self.quotes} quotes")

    def on_stop(self, context: StrategyContext) -> None:
        self.stopped = True


class LookaheadStrategy(Strategy):
    """Read one second beyond the engine clock head."""

    def __init__(self, **parameters: object) -> None:
        self.reads = 0
        self.skipped = False
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        data = self.context.data
        if data is None:
            self.skipped = True
            return
        self.reads += 1
        data.read(self.context.clock.now() + timedelta(seconds=1), event.symbol)


class ExplodingStrategy(Strategy):
    """Raise an unexpected exception on the first quote."""

    def on_quote(self, event: QuoteEvent) -> None:
        raise ValueError("strategy bug")


class RejectAllRule:
    """Veto every candidate order so rejection policies can be exercised."""

    def __init__(
        self,
        parameters: Mapping[str, object] | None = None,
        *,
        rule_id: str = "reject-all",
    ) -> None:
        self.rule_id = rule_id
        self.parameters = {} if parameters is None else dict(parameters)

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        return RuleDecision.reject(order, f"{self.rule_id} vetoed the order")


def _register_strategy_module() -> None:
    """Expose the test strategies under one importable module name."""
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (
        ("PeriodicMarketStrategy", PeriodicMarketStrategy),
        ("HaltingStrategy", HaltingStrategy),
        ("AbortingStrategy", AbortingStrategy),
        ("LookaheadStrategy", LookaheadStrategy),
        ("ExplodingStrategy", ExplodingStrategy),
        ("RejectAllRule", RejectAllRule),
    ):
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
# Abort, halt, and teardown policies
# --------------------------------------------------------------------------


def test_abort_policy_stop_returns_an_aborted_result_after_teardown() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:AbortingStrategy",
                parameters={"abort_at": "3"},
            ),
        )
    )
    options = BacktestOptions(abort_policy=AbortPolicy.STOP)

    result = _run(configuration, options=options)

    assert result.status is BacktestStatus.ABORTED
    assert result.reason is not None and "abort after 3 quotes" in result.reason
    assert result.market_event_count < result.preflight.event_count
    assert result.stopped_strategies == ("spy-momentum",)
    assert result.halted_strategies == ()


def test_abort_policy_raises_by_default_after_teardown() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:AbortingStrategy",
                parameters={"abort_at": "2"},
            ),
        )
    )

    with pytest.raises(BacktestAborted) as raised:
        _run(configuration)

    assert "abort after 2 quotes" in str(raised.value)


def test_strategy_halt_policy_halts_only_the_requesting_strategy() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                instance_id="halting",
                implementation=f"{STRATEGY_MODULE}:HaltingStrategy",
                parameters={"halt_at": "2"},
                capital_fraction=Decimal("0.5"),
            ),
            _instance(
                instance_id="trading",
                implementation=f"{STRATEGY_MODULE}:PeriodicMarketStrategy",
                parameters={"quantity": "10", "every": "4"},
                capital_fraction=Decimal("0.5"),
            ),
        )
    )

    result = _run(configuration)

    assert result.status is BacktestStatus.COMPLETED
    assert result.halted_strategies == ("halting",)
    assert result.stopped_strategies == ("halting", "trading")
    assert result.portfolio.book("trading").fill_count > 0
    assert result.portfolio.book("halting").fill_count == 0


def test_strategy_halt_policy_can_escalate_to_a_run_abort() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:HaltingStrategy",
                parameters={"halt_at": "2"},
            ),
        )
    )
    options = BacktestOptions(
        strategy_halt_policy=StrategyHaltPolicy.ABORT_RUN,
        abort_policy=AbortPolicy.STOP,
    )

    result = _run(configuration, options=options)

    assert result.status is BacktestStatus.ABORTED
    assert result.halted_strategies == ()
    assert result.stopped_strategies == ("spy-momentum",)


def test_strategy_halt_policy_can_propagate_the_original_halt() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:HaltingStrategy",
                parameters={"halt_at": "2"},
            ),
        )
    )
    options = BacktestOptions(strategy_halt_policy=StrategyHaltPolicy.RAISE)

    with pytest.raises(StrategyHalt):
        _run(configuration, options=options)


def test_teardown_policy_never_leaves_surviving_strategies_started() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                instance_id="halting",
                implementation=f"{STRATEGY_MODULE}:HaltingStrategy",
                parameters={"halt_at": "2"},
                capital_fraction=Decimal("0.5"),
            ),
            _instance(
                instance_id="trading",
                implementation=f"{STRATEGY_MODULE}:PeriodicMarketStrategy",
                parameters={"quantity": "10", "every": "4"},
                capital_fraction=Decimal("0.5"),
            ),
        )
    )
    options = BacktestOptions(teardown_policy=TeardownPolicy.NEVER)

    result = _run(configuration, options=options)

    assert result.stopped_strategies == ("halting",)
    assert "trading" not in result.stopped_strategies


def test_unexpected_strategy_errors_use_the_execution_error_taxonomy() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:ExplodingStrategy"),
        )
    )

    with pytest.raises(StrategyExecutionError) as raised:
        _run(configuration)

    assert "strategy bug" in str(raised.value)
    assert isinstance(raised.value.__cause__, ValueError)


# --------------------------------------------------------------------------
# Look-ahead guard
# --------------------------------------------------------------------------


def test_lookahead_guard_rejects_reads_beyond_the_engine_clock() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:LookaheadStrategy"),
        )
    )

    with pytest.raises(LookaheadViolationError):
        _run(configuration)


def test_lookahead_guard_can_be_disabled() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:LookaheadStrategy"),
        )
    )
    options = BacktestOptions(lookahead_guard=False)

    result = _run(configuration, options=options)

    assert result.status is BacktestStatus.COMPLETED
    assert result.lookahead_violations == ()


# --------------------------------------------------------------------------
# Fees, fills, and risk
# --------------------------------------------------------------------------


def test_fee_application_matches_exact_decimal_portfolio_accounting() -> None:
    configuration = _configuration()

    result = _run(configuration)

    assert result.fills
    assert all(fill.fee > ZERO for fill in result.fills)
    assert len(result.fee_assessments) == len(result.fills)
    fees_by_run = result.fee_totals.by_run[0]
    assert fees_by_run.currency == "USD"
    assert fees_by_run.amount == sum((fill.fee for fill in result.fills), start=ZERO)
    assert result.fee_totals.by_strategy[0].key == "spy-momentum"

    notional = sum(
        (
            fill.price * fill.quantity
            if fill.side is Side.BUY
            else -fill.price * fill.quantity
            for fill in result.fills
        ),
        start=ZERO,
    )
    book = result.portfolio.book("spy-momentum")
    assert book.cash == configuration.initial_cash - notional - fees_by_run.amount
    assert result.portfolio.cash == book.cash


def test_pessimistic_fill_plugins_are_deterministic_and_ordered() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                parameters={
                    "quantity": "10",
                    "every": "2",
                    "order_type": "limit",
                    "limit_offset": "0",
                }
            ),
        )
    )

    counts: dict[str, int] = {}
    digests: dict[str, str] = {}
    for model in (
        FillModel.SIMPLE_TOUCH,
        FillModel.LATENCY_SIZE,
        FillModel.TRADE_THROUGH,
    ):
        result = _run(configuration, options=BacktestOptions(fill_model=model))
        counts[model.value] = len(result.fills)
        digests[model.value] = result.artifact_digest()

    assert counts["simple_touch"] > 0
    assert counts["simple_touch"] >= counts["latency_size"]
    assert counts["latency_size"] >= counts["trade_through"]
    assert len(set(digests.values())) == 3


def test_risk_pipeline_halt_policy_stops_the_strategy() -> None:
    rule = (
        _example()
        .risk_pipeline.rules[0]
        .model_copy(
            update={
                "rule_id": "reject-all",
                "implementation": f"{STRATEGY_MODULE}:RejectAllRule",
                "parameters": {},
            }
        )
    )
    pipeline = _example().risk_pipeline.model_copy(
        update={"rules": (rule,), "on_reject": "halt_strategy"}
    )
    configuration = _configuration(risk_pipeline=pipeline)

    result = _run(configuration)

    assert result.status is BacktestStatus.COMPLETED
    assert result.halted_strategies == ("spy-momentum",)
    assert result.risk_decisions
    assert all(decision.rejected for decision in result.risk_decisions)
    assert result.fills == ()


def test_risk_pipeline_rejects_orders_without_halting_by_default() -> None:
    rule = (
        _example()
        .risk_pipeline.rules[0]
        .model_copy(
            update={
                "rule_id": "reject-all",
                "implementation": f"{STRATEGY_MODULE}:RejectAllRule",
                "parameters": {},
            }
        )
    )
    pipeline = _example().risk_pipeline.model_copy(update={"rules": (rule,)})
    configuration = _configuration(risk_pipeline=pipeline)

    result = _run(configuration)

    assert result.status is BacktestStatus.COMPLETED
    assert result.risk_decisions
    assert all(decision.rejected for decision in result.risk_decisions)
    assert result.fills == ()
    assert result.halted_strategies == ()
    assert result.stopped_strategies == ("spy-momentum",)


def test_risk_pipeline_scales_oversized_orders_to_the_configured_limit() -> None:
    limit = Decimal("1000.00")
    rule = (
        _example()
        .risk_pipeline.rules[0]
        .model_copy(update={"parameters": {"limit": limit}})
    )
    pipeline = _example().risk_pipeline.model_copy(update={"rules": (rule,)})
    configuration = _configuration(risk_pipeline=pipeline)

    result = _run(configuration)

    assert result.risk_decisions
    assert all(decision.reduced for decision in result.risk_decisions)
    reduced = 0
    for decision in result.risk_decisions:
        order = decision.order
        original = decision.original_order
        if not isinstance(order, SingleLegOrder) or not isinstance(
            original, SingleLegOrder
        ):
            continue
        assert order.quantity < original.quantity
        reduced += 1
    assert reduced == len(result.risk_decisions)
    assert result.fills
    assert all(fill.quantity < Decimal("10") for fill in result.fills)
    assert limit == Decimal("1000.00")


# --------------------------------------------------------------------------
# Configuration and strategy loading errors
# --------------------------------------------------------------------------


def test_run_backtest_rejects_a_non_backtest_configuration() -> None:
    trader = load_example("trader")

    with pytest.raises(BacktestConfigurationError):
        run_backtest(cast("BacktestConfig", trader))


def test_strategy_loading_errors_use_the_loader_taxonomy() -> None:
    configuration = _configuration(
        strategy_instances=(_instance(implementation="not_a_module_path"),)
    )

    with pytest.raises(StrategyLoadError):
        _run(configuration)


def test_missing_strategy_implementation_raises_strategy_load_error() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:AbsentStrategy"),
        )
    )

    with pytest.raises(StrategyLoadError):
        _run(configuration)
