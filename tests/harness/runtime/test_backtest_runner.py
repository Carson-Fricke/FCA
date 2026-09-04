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
from harness.core.clock import ReadonlyClock, SimClock
from harness.core.types import (
    AccountSnapshot,
    Book,
    Event,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
)
from harness.data import HistoricalView
from harness.data.history import StrategyHistory
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
from harness.strategy import Scheduler, Strategy, StrategyContext

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
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        self.reads += 1
        data = self.context.data
        assert isinstance(data, StrategyHistory)
        head = self.context.clock.now()
        data.window(event.symbol, head, head + timedelta(seconds=1))


class VenueProbingStrategy(Strategy):
    """Probe every strategy-visible venue surface for venue-state attributes.

    The engine hands strategies a one-way command capability. If any venue
    state attribute were reachable, the probe raises and the run aborts with
    the execution-error taxonomy; a green run proves the strategy-visible
    surfaces expose no venue state.
    """

    forbidden_attributes = (
        "remaining_steps",
        "calls",
        "state",
        "decisions_for",
        "active_orders",
        "latest_market",
        "steps",
    )

    def __init__(self, **parameters: object) -> None:
        self.context: StrategyContext | None = None
        self.observations: list[str] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe("on_start")

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe("on_quote")

    def on_stop(self, context: StrategyContext) -> None:
        self._probe("on_stop")

    def _probe(self, phase: str) -> None:
        context = self.context
        if context is None:
            raise RuntimeError("venue probe has not received on_start")
        surfaces: dict[str, object] = {
            "venue": context.venue,
            "orders": context.orders,
            "orders.venue": context.orders.venue,
        }
        for surface_name, surface in surfaces.items():
            if surface is None:
                continue
            for attribute in self.forbidden_attributes:
                if hasattr(surface, attribute):
                    self.observations.append(f"{surface_name}.{attribute}")
        if self.observations:
            raise ValueError(f"strategy observed venue state: {self.observations}")


class ScheduleFabricatingStrategy(Strategy):
    """Schedule one reserved feed or outcome event through the context.

    Feed and outcome event classes are reserved for the data feed and the
    venue. The scheduler capability must refuse them before they reach the
    event bus, so fabricated market data or execution outcomes are
    structurally unrepresentable from strategy code.
    """

    def __init__(self, **parameters: object) -> None:
        self.event_kind = str(parameters.get("event_kind", "quote"))
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        self.context.schedule(_fabricated_event(self.event_kind, event))


class CapabilityProbeStrategy(Strategy):
    """Probe every strategy-visible surface for engine-internal handles.

    The engine hands strategies capability objects only: a read-only clock,
    the opaque :class:`StrategyHistory` data capability, a one-way venue
    command boundary, and a scheduler capability. If any engine-internal
    surface (clock advancement, raw history view, head access) were
    reachable, the probe raises and the run aborts; a green run proves the
    callback surface is capability-narrow.
    """

    forbidden_attributes = (
        ("clock", "advance_to"),
        ("clock", "advance_by"),
        ("clock", "head"),
        ("data", "record"),
        ("data", "record_many"),
        ("data", "subscribe"),
        ("data", "attach"),
        ("data", "head"),
        ("data", "capacity"),
        ("data", "subscriptions"),
    )

    def __init__(self, **parameters: object) -> None:
        self.context: StrategyContext | None = None
        self.observations: list[str] = []

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        self._probe()

    def on_quote(self, event: QuoteEvent) -> None:
        self._probe()

    def on_stop(self, context: StrategyContext) -> None:
        self._probe()

    def _probe(self) -> None:
        context = self.context
        if context is None:
            raise RuntimeError("capability probe has not received on_start")
        clock = context.clock
        if isinstance(clock, SimClock) or not isinstance(clock, ReadonlyClock):
            self.observations.append("clock is not a read-only capability")
        data = context.data
        if isinstance(data, HistoricalView) or not isinstance(data, StrategyHistory):
            self.observations.append("data is not the opaque history capability")
        for surface_name, attribute in self.forbidden_attributes:
            surface = clock if surface_name == "clock" else data
            if surface is not None and hasattr(surface, attribute):
                self.observations.append(f"{surface_name}.{attribute}")
        if not isinstance(context.scheduler, Scheduler):
            self.observations.append("scheduler is not the Scheduler capability")
        if self.observations:
            raise ValueError(f"strategy observed engine internals: {self.observations}")


class InertStrategy(Strategy):
    """Observe events without acting, for dispatch-stream comparisons."""

    def on_quote(self, event: QuoteEvent) -> None:
        return


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


def _fabricated_event(kind: str, template: QuoteEvent) -> Event:
    """Build one reserved feed or outcome event stamped at the current head."""
    stamp = template.timestamp
    sequence = template.sequence
    symbol = template.symbol
    if kind == "quote":
        return QuoteEvent(
            event_id="fabricated-quote",
            timestamp=stamp,
            sequence=sequence,
            symbol=symbol,
            bid_price=Decimal("99"),
            bid_quantity=Decimal("1"),
            ask_price=Decimal("101"),
            ask_quantity=Decimal("1"),
        )
    if kind == "trade":
        return TradeEvent(
            event_id="fabricated-trade",
            timestamp=stamp,
            sequence=sequence,
            symbol=symbol,
            price=Decimal("100"),
            quantity=Decimal("1"),
        )
    if kind == "execution_report":
        return ExecutionReport(
            event_id="fabricated-report",
            timestamp=stamp,
            sequence=sequence,
            report_id="fabricated-report-1",
            order_id="order-1",
            status=OrderStatus.NEW,
            execution_type=ExecutionType.ACKNOWLEDGED,
            order_quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("1"),
        )
    if kind == "fill":
        return Fill(
            event_id="fabricated-fill",
            timestamp=stamp,
            sequence=sequence,
            fill_id="fabricated-fill-1",
            order_id="order-1",
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
        )
    if kind == "position":
        return Position(
            event_id="fabricated-position",
            timestamp=stamp,
            sequence=sequence,
            account_id="account-1",
            symbol=symbol,
            quantity=Decimal("1"),
            average_price=Decimal("100"),
        )
    if kind == "book":
        return Book(
            event_id="fabricated-book",
            timestamp=stamp,
            sequence=sequence,
            symbol=symbol,
        )
    if kind == "account":
        return AccountSnapshot(
            event_id="fabricated-account",
            timestamp=stamp,
            sequence=sequence,
            account_id="account-1",
            cash=Decimal("100"),
            equity=Decimal("100"),
            buying_power=Decimal("100"),
        )
    raise AssertionError(f"unknown fabricated event kind: {kind}")


def _register_strategy_module() -> None:
    """Expose the test strategies under one importable module name."""
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (
        ("PeriodicMarketStrategy", PeriodicMarketStrategy),
        ("HaltingStrategy", HaltingStrategy),
        ("AbortingStrategy", AbortingStrategy),
        ("LookaheadStrategy", LookaheadStrategy),
        ("VenueProbingStrategy", VenueProbingStrategy),
        ("ScheduleFabricatingStrategy", ScheduleFabricatingStrategy),
        ("CapabilityProbeStrategy", CapabilityProbeStrategy),
        ("InertStrategy", InertStrategy),
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
# Look-ahead capability boundary
# --------------------------------------------------------------------------


def test_lookahead_reads_beyond_the_engine_clock_abort_the_run() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:LookaheadStrategy"),
        )
    )

    with pytest.raises(LookaheadViolationError):
        _run(configuration)


def test_lookahead_violations_are_recorded_on_the_aborted_result() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:LookaheadStrategy"),
        )
    )
    options = BacktestOptions(abort_policy=AbortPolicy.STOP)

    result = _run(configuration, options=options)

    assert result.status is BacktestStatus.ABORTED
    assert result.lookahead_violations == ("spy-momentum",)
    assert result.reason is not None and "ahead of the engine clock" in result.reason


@pytest.mark.parametrize(
    "event_kind",
    ["quote", "trade", "execution_report", "fill", "position", "book", "account"],
)
def test_strategy_cannot_schedule_reserved_feed_or_outcome_events(
    event_kind: str,
) -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:ScheduleFabricatingStrategy",
                parameters={"event_kind": event_kind},
            ),
        )
    )

    with pytest.raises(StrategyExecutionError) as raised:
        _run(configuration)

    cause = raised.value.__cause__
    assert isinstance(cause, TypeError)
    assert "reserved" in str(cause)


def test_capability_surfaces_expose_no_engine_internals() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:CapabilityProbeStrategy"),
        )
    )

    result = _run(configuration)

    assert result.status is BacktestStatus.COMPLETED
    assert result.stopped_strategies == ("spy-momentum",)


# --------------------------------------------------------------------------
# Venue capability boundary
# --------------------------------------------------------------------------


def test_venue_capability_denies_venue_state_introspection() -> None:
    configuration = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:VenueProbingStrategy"),
        )
    )

    result = _run(configuration)

    assert result.status is BacktestStatus.COMPLETED
    assert result.fills == ()
    assert result.stopped_strategies == ("spy-momentum",)


def test_venue_introspection_attempts_do_not_change_the_dispatch_stream() -> None:
    probing = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:VenueProbingStrategy"),
        )
    )
    inert = _configuration(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:InertStrategy"),
        )
    )

    probe_result = _run(probing)
    repeat_result = _run(probing)
    inert_result = _run(inert)

    assert probe_result.dispatch_hash == repeat_result.dispatch_hash
    assert probe_result.dispatch_hash == inert_result.dispatch_hash


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
