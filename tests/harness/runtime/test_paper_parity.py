"""PaperParity harness tests: parity scenarios, reports, and error taxonomy."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from harness.configuration import (
    RiskPipelineConfig,
    RiskRuleConfig,
    StrategyInstanceConfig,
    TraderConfig,
    load_example,
)
from harness.core.symbols import Symbol
from harness.core.types import (
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
)
from harness.data import LakeReplayFeed
from harness.risk import RiskAction, RiskPipeline, RuleDecision
from harness.runtime.backtest import BacktestStatus, DataPreflightError
from harness.runtime.paper import PaperSessionStatus, ReplayPaperFeed
from harness.strategy import Strategy, StrategyContext
from harness.testing.paper_parity import (
    PaperParityConfigurationError,
    PaperParityError,
    PaperParityScenario,
    ParityDivergence,
    assert_paper_parity,
    compare_paper_backtest,
    run_paper_parity,
)
from lake.contracts import ExchangeSession

STRATEGY_MODULE = "fca_paper_parity_test_strategies"
BUNDLE = "c" * 64
RUN_ID = "paper-parity-1"
CASH = Decimal("100000")
INSTANCE_ID = "solo"
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)

_TRADER_EXAMPLE = load_example("trader")
assert isinstance(_TRADER_EXAMPLE, TraderConfig)
FEES = _TRADER_EXAMPLE.fees
RISK = RiskPipelineConfig(
    rules=(
        RiskRuleConfig(
            rule_id="parity-max-order",
            implementation="harness.risk.limits:MaxOrderNotional",
            parameters={"limit": Decimal("1000000")},
        ),
    ),
    on_reject="reject_order",
)


# --------------------------------------------------------------------------
# Test strategies
# --------------------------------------------------------------------------


class PeriodicMarketStrategy(Strategy):
    """Submit one deterministic order every ``every`` quotes."""

    def __init__(self, **parameters: object) -> None:
        self.quantity = Decimal(str(parameters.get("quantity", "10")))
        self.every = int(str(parameters.get("every", "2")))
        self.prefix = str(parameters.get("prefix", "order"))
        self.quotes = 0

    def on_quote(self, event: QuoteEvent) -> None:
        self.quotes += 1
        if self.quotes % self.every:
            return
        assert self.context is not None
        side = Side.BUY if (self.quotes // self.every) % 2 else Side.SELL
        order = SingleLegOrder(
            event_id=f"{self.prefix}-{self.quotes}",
            timestamp=event.timestamp,
            sequence=event.sequence,
            order_id=f"{self.prefix}-{self.quotes}",
            symbol=event.symbol,
            side=side,
            quantity=self.quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
        )
        self.context.orders.submit(order)

    def on_start(self, context: StrategyContext) -> None:
        self.context = context


class HaltingStrategy(Strategy):
    """Raise :class:`StrategyHalt` after a deterministic number of quotes."""

    def __init__(self, **parameters: object) -> None:
        self.halt_at = int(str(parameters.get("halt_at", "2")))
        self.quotes = 0

    def on_quote(self, event: QuoteEvent) -> None:
        from harness.runtime.backtest import StrategyHalt

        self.quotes += 1
        if self.quotes >= self.halt_at:
            raise StrategyHalt(f"halt after {self.quotes} quotes")


class AbortingStrategy(Strategy):
    """Raise :class:`BacktestAbort` after a deterministic number of quotes."""

    def __init__(self, **parameters: object) -> None:
        self.abort_at = int(str(parameters.get("abort_at", "3")))
        self.quotes = 0

    def on_quote(self, event: QuoteEvent) -> None:
        from harness.runtime.backtest import BacktestAbort

        self.quotes += 1
        if self.quotes >= self.abort_at:
            raise BacktestAbort(f"abort after {self.quotes} quotes")


class LookaheadStrategy(Strategy):
    """Read one minute beyond the engine clock head."""

    def __init__(self, **parameters: object) -> None:
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        data = self.context.data
        if data is None:
            raise RuntimeError("lookahead strategy requires a data boundary")
        head = self.context.clock.now()
        cast(Any, data).window(event.symbol, head, head + timedelta(minutes=1))


class RejectAllRule:
    """Veto every candidate order so rejection behavior can be exercised."""

    def __init__(
        self,
        parameters: dict[str, object] | None = None,
        *,
        rule_id: str = "reject-all",
    ) -> None:
        self.rule_id = rule_id

    def evaluate(self, order: Any, context: Any) -> RuleDecision:
        return RuleDecision.reject(order, f"{self.rule_id} vetoed the order")


def _register_strategy_module() -> None:
    """Expose the test strategies under one importable module name."""
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (
        ("PeriodicMarketStrategy", PeriodicMarketStrategy),
        ("HaltingStrategy", HaltingStrategy),
        ("AbortingStrategy", AbortingStrategy),
        ("LookaheadStrategy", LookaheadStrategy),
    ):
        setattr(module, name, value)
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def _events() -> list[QuoteEvent | TradeEvent]:
    """Build one deterministic three-hour quote and trade stream."""
    events: list[QuoteEvent | TradeEvent] = []
    sequence = 0
    for index in range(61):
        moment = OPEN + timedelta(minutes=3 * index)
        mid = Decimal("450.00") + Decimal("0.01") * (index % 40)
        sequence += 1
        events.append(
            QuoteEvent(
                event_id=f"quote-{index}",
                timestamp=moment,
                sequence=sequence,
                symbol=Symbol("SPY"),
                bid_price=mid - Decimal("0.01"),
                bid_quantity=Decimal("100"),
                ask_price=mid + Decimal("0.01"),
                ask_quantity=Decimal("100"),
                venue="XNYS",
            )
        )
        if index % 3 == 0:
            sequence += 1
            events.append(
                TradeEvent(
                    event_id=f"trade-{index}",
                    timestamp=moment + timedelta(seconds=90),
                    sequence=sequence,
                    symbol=Symbol("SPY"),
                    price=mid,
                    quantity=Decimal("50"),
                    venue="XNYS",
                    trade_id=str(1_000_000 + index),
                )
            )
    return events


def _quote(moment: datetime, sequence: int, symbol: str = "SPY") -> QuoteEvent:
    return QuoteEvent(
        event_id=f"quote-{symbol}-{moment.isoformat(timespec='microseconds')}",
        timestamp=moment,
        sequence=sequence,
        symbol=Symbol(symbol),
        bid_price=Decimal("450.00"),
        bid_quantity=Decimal("100"),
        ask_price=Decimal("450.02"),
        ask_quantity=Decimal("100"),
        venue="XNYS",
    )


def _instance(
    instance_id: str = INSTANCE_ID,
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


def _trader_config(**overrides: object) -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    updates: dict[str, object] = {
        "mode": "paper",
        "seed": 7,
        "strategy_instances": (_instance(),),
        "risk_pipeline": RISK,
        "fees": FEES,
    }
    updates.update(overrides)
    return example.model_copy(update=updates)


def _scenario(**overrides: object) -> PaperParityScenario:
    values: dict[str, object] = {
        "configuration": _trader_config(),
        "events": _events(),
        "run_id": RUN_ID,
        "initial_cash": CASH,
    }
    values.update(overrides)
    return PaperParityScenario(**cast(dict[str, Any], values))


# --------------------------------------------------------------------------
# Parity scenarios
# --------------------------------------------------------------------------


def test_parity_harness_pins_the_full_ledger() -> None:
    report = _scenario().run()

    assert report.parity_clean
    assert report.first_divergence is None
    assert all(dimension.matched for dimension in report.dimensions)
    paper = report.paper
    backtest = report.backtest
    assert paper.status is PaperSessionStatus.COMPLETED
    assert backtest.status is BacktestStatus.COMPLETED
    assert paper.market_event_count == backtest.market_event_count == len(_events())
    assert paper.order_count == backtest.order_count > 0
    assert paper.fills == backtest.fills
    assert paper.fills
    assert paper.fee_assessments == backtest.fee_assessments
    assert paper.portfolio == backtest.portfolio
    assert paper.dispatch_hash == backtest.dispatch_hash
    assert paper.risk_decisions == backtest.risk_decisions
    assert paper.halted_strategies == backtest.halted_strategies == ()
    assert report.scenario_hash is not None and len(report.scenario_hash) == 64


def test_parity_report_documents_legitimate_differences() -> None:
    report = _scenario().run()

    assert report.parity_clean
    excluded = report.excluded_dimensions
    assert "provenance.booted_at_ended_at" in excluded
    assert "provenance.mode_and_engine_id" in excluded
    assert "policy.extra_latency" in excluded
    assert report.paper.mode == "paper"
    assert report.backtest.manifest.engine_id == "harness.backtest"
    assert report.paper.booted_at == OPEN
    assert report.paper.ended_at == OPEN
    assert report.paper.session is None
    assert report.paper.feed_fingerprints is not None
    assert (
        report.paper.feed_fingerprints.data
        == report.backtest.preflight.fingerprints.data
    )


def test_scenario_hash_pins_scenario_inputs() -> None:
    assert _scenario().scenario_hash == _scenario().scenario_hash
    divergent = _trader_config(
        strategy_instances=(_instance(parameters={"quantity": "25"}),)
    )
    assert _scenario().scenario_hash != _scenario(configuration=divergent).scenario_hash


def test_halt_scenario_stays_in_parity() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:HaltingStrategy",
                parameters={"halt_at": "2"},
            ),
        )
    )
    report = assert_paper_parity(_scenario(configuration=config))

    assert report.parity_clean
    assert report.paper.status is PaperSessionStatus.COMPLETED
    assert report.backtest.status is BacktestStatus.COMPLETED
    assert (
        report.paper.halted_strategies
        == report.backtest.halted_strategies
        == (INSTANCE_ID,)
    )
    assert report.paper.stopped_strategies == (INSTANCE_ID,)
    assert report.paper.teardown_errors == ()


def test_abort_scenario_parity() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:AbortingStrategy",
                parameters={"abort_at": "3"},
            ),
        )
    )
    report = assert_paper_parity(_scenario(configuration=config))

    assert report.parity_clean
    assert report.paper.status is PaperSessionStatus.ABORTED
    assert report.backtest.status is BacktestStatus.ABORTED
    assert report.paper.reason == report.backtest.reason
    assert "abort after 3 quotes" in cast(str, report.paper.reason)
    assert (
        report.paper.market_event_count
        == report.backtest.market_event_count
        < len(_events())
    )


def test_risk_rejection_scenario_parity() -> None:
    report = assert_paper_parity(
        _scenario(risk_pipeline=RiskPipeline((RejectAllRule(),)))
    )

    assert report.parity_clean
    assert report.paper.fills == () == report.backtest.fills
    assert report.paper.risk_decisions == report.backtest.risk_decisions
    assert report.paper.risk_decisions
    assert all(
        decision.action is RiskAction.REJECT for decision in report.paper.risk_decisions
    )
    assert report.paper.portfolio.book(INSTANCE_ID).cash == CASH
    assert report.backtest.portfolio.book(INSTANCE_ID).cash == CASH


def test_multi_strategy_routing_parity() -> None:
    instances = (
        _instance(
            "alpha",
            parameters={"every": "2", "prefix": "alpha"},
            capital_fraction=Decimal("0.6"),
        ),
        _instance(
            "beta",
            parameters={"every": "3", "prefix": "beta"},
            capital_fraction=Decimal("0.4"),
        ),
    )
    report = assert_paper_parity(
        _scenario(configuration=_trader_config(strategy_instances=instances))
    )

    assert report.parity_clean
    assert tuple(book.book_id for book in report.paper.portfolio.books) == (
        "alpha",
        "beta",
    )
    assert report.paper.portfolio == report.backtest.portfolio
    assert {assessment.strategy_id for assessment in report.paper.fee_assessments} == {
        "alpha",
        "beta",
    }
    assert report.paper.fee_assessments == report.backtest.fee_assessments


def test_lookahead_scenario_parity() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:LookaheadStrategy"),
        )
    )
    report = assert_paper_parity(_scenario(configuration=config))

    assert report.parity_clean
    assert report.paper.status is PaperSessionStatus.ABORTED
    assert report.backtest.status is BacktestStatus.ABORTED
    assert (
        report.paper.lookahead_violations
        == (report.backtest.lookahead_violations)
        == (INSTANCE_ID,)
    )


# --------------------------------------------------------------------------
# Reports, divergence taxonomy, and configuration errors
# --------------------------------------------------------------------------


def test_compare_flags_first_fill_divergence_with_focused_detail() -> None:
    clean = _scenario().run()
    divergent = _scenario(
        configuration=_trader_config(
            strategy_instances=(_instance(parameters={"quantity": "25"}),)
        )
    ).run()

    report = compare_paper_backtest(clean.paper, divergent.backtest)

    assert not report.parity_clean
    assert report.first_divergence is ParityDivergence.FILLS
    assert report.divergences == (
        ParityDivergence.FILLS,
        ParityDivergence.FEES,
        ParityDivergence.PORTFOLIO,
        ParityDivergence.RISK,
    )
    fill_dimension = next(
        dimension for dimension in report.dimensions if dimension.name == "fills"
    )
    assert fill_dimension.detail is not None
    assert "fills[0]" in fill_dimension.detail
    assert "quantity" in fill_dimension.detail
    assert report.paper.dispatch_hash == report.backtest.dispatch_hash


def test_report_assert_clean_raises_with_focused_summary() -> None:
    clean = _scenario().run()
    divergent = _scenario(
        configuration=_trader_config(
            strategy_instances=(_instance(parameters={"quantity": "25"}),)
        )
    ).run()

    divergent_report = compare_paper_backtest(clean.paper, divergent.backtest)
    with pytest.raises(PaperParityError, match="fills"):
        divergent_report.assert_clean()

    clean_report = compare_paper_backtest(clean.paper, clean.backtest)
    assert clean_report.summary().startswith(f"paper/backtest parity clean ({RUN_ID})")


def test_compare_rejects_run_id_mismatch() -> None:
    clean = _scenario().run()
    other_run = _scenario(run_id="other-run").run()

    with pytest.raises(PaperParityConfigurationError, match="run_id mismatch"):
        compare_paper_backtest(clean.paper, other_run.backtest)


def test_scenario_validates_configuration() -> None:
    with pytest.raises(PaperParityConfigurationError):
        PaperParityScenario(cast(Any, load_example("backtest")), _events())
    with pytest.raises(PaperParityConfigurationError):
        _scenario(configuration=_trader_config(mode="shadow"))
    with pytest.raises(PaperParityConfigurationError):
        _scenario(events=())
    with pytest.raises(PaperParityConfigurationError):
        _scenario(events=(cast(Any, object()),))
    with pytest.raises(PaperParityConfigurationError):
        _scenario(run_id="   ")
    with pytest.raises(PaperParityConfigurationError):
        _scenario(initial_cash=Decimal("0"))
    with pytest.raises(PaperParityConfigurationError):
        _scenario(start=OPEN + timedelta(minutes=3))
    with pytest.raises(PaperParityConfigurationError):
        _scenario(end=OPEN + timedelta(minutes=6))
    with pytest.raises(PaperParityConfigurationError):
        _scenario(snapshot_fingerprint="not-a-digest")
    with pytest.raises(PaperParityConfigurationError):
        _scenario(
            events=(_quote(OPEN + timedelta(minutes=3), 2), _quote(OPEN, 1)),
        )
    msft_events = [
        _quote(OPEN, 1, "MSFT"),
        _quote(OPEN + timedelta(minutes=3), 2, "MSFT"),
    ]
    with pytest.raises(PaperParityConfigurationError, match="SPY"):
        _scenario(events=msft_events)


def test_report_artifacts_are_json_ready() -> None:
    report = _scenario().run()

    payload = json.loads(json.dumps(report.artifacts()))

    assert payload["schema"] == "fca-paper-parity-report-v1"
    assert payload["run_id"] == RUN_ID
    assert payload["parity_clean"] is True
    assert payload["first_divergence"] is None
    assert {dimension["name"] for dimension in payload["dimensions"]} == {
        divergence.value for divergence in ParityDivergence
    }
    assert payload["paper"]["status"] == "completed"
    assert payload["backtest"]["status"] == "completed"
    assert payload["paper"]["counts"] == payload["backtest"]["counts"]
    assert "provenance.booted_at_ended_at" in payload["excluded_dimensions"]


def test_parity_scenarios_pin_snapshot_identity() -> None:
    window = {"start": OPEN, "end": OPEN + timedelta(days=2)}
    baseline = _scenario(session_date=date(2024, 1, 2), **window).run()
    paper_fingerprints = baseline.paper.feed_fingerprints
    assert paper_fingerprints is not None

    scenario = _scenario(
        session_date=date(2024, 1, 2),
        snapshot_fingerprint=paper_fingerprints.data,
        snapshot_id="parity-snapshot",
        **window,
    )
    report = scenario.run()

    assert report.parity_clean
    assert report.backtest.manifest.data_fingerprint == paper_fingerprints.data
    assert report.backtest.manifest.data_snapshot_id == "parity-snapshot"

    with pytest.raises(DataPreflightError):
        _scenario(snapshot_fingerprint="a" * 64, snapshot_id="pinned").run()


def test_default_snapshot_identity_is_derived_from_the_stream() -> None:
    report = _scenario().run()

    assert report.backtest.manifest.data_snapshot_id == f"paper-parity-{RUN_ID}"
    assert (
        report.backtest.manifest.data_fingerprint
        == report.backtest.preflight.fingerprints.data
    )


def test_harness_functions_reject_wrong_types() -> None:
    with pytest.raises(TypeError):
        run_paper_parity(cast(Any, object()))
    with pytest.raises(TypeError):
        compare_paper_backtest(cast(Any, object()), cast(Any, object()))


def test_paper_session_and_backtest_share_replay_semantics() -> None:
    events = _events()
    scenario = _scenario()
    report = scenario.run()
    feed = ReplayPaperFeed(
        LakeReplayFeed(
            (ExchangeSession(date(2024, 1, 2), OPEN, scenario.effective_end),),
            streams={"SPY": list(events)},
        )
    )
    consumed = 0
    while not feed.exhausted:
        if feed.next_event() is not None:
            consumed += 1
    assert consumed == len(events) == report.paper.market_event_count
