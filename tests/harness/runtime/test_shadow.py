"""ShadowShell tests: observe-only routing, side-by-side simulation, parity."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

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
    MultiLegOrder,
    OrderLeg,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.runtime.paper import (
    PaperFeed,
    PaperOptions,
    PaperSessionPolicy,
    PaperSessionStatus,
    run_paper_session,
)
from harness.runtime.shadow import (
    BrokerIntent,
    LivePrint,
    ObserveOnlyBrokerSink,
    ShadowConfigurationError,
    ShadowReport,
    ShadowShell,
    ShadowVerdict,
    run_shadow_session,
)
from harness.strategy import Strategy, StrategyContext

STRATEGY_MODULE = "fca_shadow_test_strategies"
BUNDLE = "b" * 64
RUN_ID = "shadow-1"
CASH = Decimal("100000")
INSTANCE_ID = "solo"
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
QUOTES = 8

_TRADER_EXAMPLE = load_example("trader")
assert isinstance(_TRADER_EXAMPLE, TraderConfig)
FEES = _TRADER_EXAMPLE.fees
RISK = RiskPipelineConfig(
    rules=(
        RiskRuleConfig(
            rule_id="shadow-max-order",
            implementation="harness.risk.limits:MaxOrderNotional",
            parameters={"limit": Decimal("1000000")},
        ),
    ),
    on_reject="reject_order",
)


# --------------------------------------------------------------------------
# Test strategies and feeds
# --------------------------------------------------------------------------


class PeriodicMarketStrategy(Strategy):
    """Submit one deterministic market order every ``every`` quotes."""

    def __init__(self, **parameters: object) -> None:
        self.quantity = Decimal(str(parameters.get("quantity", "10")))
        self.every = int(str(parameters.get("every", "2")))
        self.prefix = str(parameters.get("prefix", "order"))
        self.context: StrategyContext | None = None
        self.quotes = 0

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

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


class RestingLimitStrategy(Strategy):
    """Submit one non-marketable limit order on the first quote."""

    def __init__(self, **parameters: object) -> None:
        self.prefix = str(parameters.get("prefix", "rest"))
        self.context: StrategyContext | None = None
        self.done = False

    def on_start(self, context: StrategyContext) -> None:
        self.context = context

    def on_quote(self, event: QuoteEvent) -> None:
        if self.done:
            return
        self.done = True
        assert self.context is not None
        order = SingleLegOrder(
            event_id=f"{self.prefix}-1",
            timestamp=event.timestamp,
            sequence=event.sequence,
            order_id=f"{self.prefix}-1",
            symbol=event.symbol,
            side=Side.BUY,
            quantity=Decimal("5"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=Decimal("1.00"),
        )
        self.context.orders.submit(order)


def _register_strategy_module() -> None:
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (
        ("PeriodicMarketStrategy", PeriodicMarketStrategy),
        ("RestingLimitStrategy", RestingLimitStrategy),
    ):
        setattr(module, name, value)
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


class _ScriptedLiveFeed:
    """Live-shaped boundary over scripted events; exhausts for the test."""

    def __init__(self, events: list[QuoteEvent]) -> None:
        self._events = list(events)
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def next_event(self) -> QuoteEvent | None:
        if not self._events:
            self._exhausted = True
            return None
        return self._events.pop(0)

    def close(self) -> None:
        self._exhausted = True


class _IdlingLiveFeed:
    """True live boundary shape: never exhausts, idles with ``None``."""

    def __init__(self, events: list[QuoteEvent]) -> None:
        self._events = list(events)
        self.closed = False

    @property
    def exhausted(self) -> bool:
        return False

    def next_event(self) -> QuoteEvent | None:
        return self._events.pop(0) if self._events else None

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def _events() -> list[QuoteEvent]:
    return [
        QuoteEvent(
            event_id=f"quote-{index}",
            timestamp=OPEN + timedelta(minutes=index),
            sequence=index + 1,
            symbol=Symbol("SPY"),
            bid_price=Decimal("449.99"),
            bid_quantity=Decimal("100"),
            ask_price=Decimal("450.01"),
            ask_quantity=Decimal("100"),
            venue="XNYS",
        )
        for index in range(QUOTES)
    ]


def _instance() -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=INSTANCE_ID,
        implementation=f"{STRATEGY_MODULE}:PeriodicMarketStrategy",
        bundle_hash=BUNDLE,
        capital_fraction=Decimal("1.0"),
        symbols=("SPY",),
    )


def _example_config() -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    return example


def _trader_config(mode: str = "shadow") -> TraderConfig:
    return _example_config().model_copy(
        update={
            "mode": mode,
            "seed": 7,
            "strategy_instances": (_instance(),),
            "risk_pipeline": RISK,
            "fees": FEES,
        }
    )


def _options(
    *, run_id: str = RUN_ID, idle_pace: Callable[[], None] | None = None
) -> PaperOptions:
    return PaperOptions(
        run_id=run_id,
        initial_cash=CASH,
        session_policy=PaperSessionPolicy.OPEN_ENDED,
        start=OPEN,
        idle_pace=idle_pace,
    )


def _shadow_shell(
    *, mode: str = "shadow", options: PaperOptions | None = None
) -> ShadowShell:
    return ShadowShell(
        _trader_config(mode), options=_options() if options is None else options
    )


def _shadow_report() -> ShadowReport:
    return _shadow_shell().run(_ScriptedLiveFeed(_events()))


def _prints_for(report: ShadowReport) -> list[LivePrint]:
    return [
        LivePrint(
            order_id=fill.order_id,
            symbol=str(fill.symbol),
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            timestamp=fill.timestamp,
        )
        for fill in report.session.fills
    ]


# --------------------------------------------------------------------------
# Observe-only broker sink
# --------------------------------------------------------------------------


def test_sink_records_intents_without_transmission() -> None:
    sink = ObserveOnlyBrokerSink()
    order = SingleLegOrder(
        event_id="e1",
        timestamp=OPEN,
        sequence=1,
        order_id="order-1",
        symbol=Symbol("SPY"),
        side=Side.BUY,
        quantity=Decimal("10"),
    )

    intent = sink.submit(order, strategy_id=INSTANCE_ID)

    assert isinstance(intent, BrokerIntent)
    assert intent.sequence == 1
    assert intent.order_id == "order-1"
    assert intent.strategy_id == INSTANCE_ID
    assert intent.order == order
    second = sink.submit(order)
    assert second.sequence == 2 and second.strategy_id is None
    assert sink.intents == (intent, second)
    assert len(sink) == 2
    assert not any(
        hasattr(sink, name) for name in ("send", "transmit", "post", "connect")
    )


def test_sink_refuses_multi_leg_orders() -> None:
    sink = ObserveOnlyBrokerSink()
    leg = OrderLeg(symbol=Symbol("SPY"), side=Side.BUY, quantity=Decimal("10"))
    order = MultiLegOrder(
        event_id="e2",
        timestamp=OPEN,
        sequence=1,
        order_id="spread-1",
        legs=(leg, leg),
    )

    with pytest.raises(ShadowConfigurationError):
        sink.submit(order)
    assert sink.intents == ()


# --------------------------------------------------------------------------
# Shadow sessions
# --------------------------------------------------------------------------


def test_shadow_session_records_every_intent_and_comparison() -> None:
    shell = _shadow_shell()
    report = shell.run(_ScriptedLiveFeed(_events()))

    assert report.session.status is PaperSessionStatus.COMPLETED
    assert report.deployment_mode == "shadow"
    assert report.intents == shell.sink.intents
    assert len(report.intents) == report.session.order_count > 0
    assert len(report.comparisons) == len(report.intents)
    for comparison in report.comparisons:
        assert comparison.verdict is ShadowVerdict.WOULD_BE_ONLY
        assert comparison.live is None
        assert comparison.would_be
    assert report.would_be_only_count == len(report.intents)
    assert report.matched_count == 0
    assert report.diverged_count == 0
    assert report.live_only_count == 0
    assert report.unfilled_count == 0
    assert report.parity_clean
    assert report.summary().startswith("shadow parity clean")


def test_shadow_session_is_ledger_identical_to_paper() -> None:
    events = _events()
    report = _shadow_shell(mode="paper").run(_ScriptedLiveFeed(list(events)))
    plain = run_paper_session(
        _trader_config(mode="paper"),
        _ScriptedLiveFeed(list(events)),
        options=_options(),
    )

    assert report.session.dispatch_hash == plain.dispatch_hash
    assert report.session.dispatched_count == plain.dispatched_count
    assert report.session.market_event_count == plain.market_event_count
    assert report.session.order_count == plain.order_count
    assert report.session.fills == plain.fills
    assert report.session.fee_assessments == plain.fee_assessments
    assert report.session.risk_decisions == plain.risk_decisions
    assert report.session.portfolio == plain.portfolio
    assert len(report.intents) == plain.order_count


def test_shadow_intents_match_approved_risk_decisions() -> None:
    report = _shadow_report()

    approved_ids = {
        decision.order.order_id
        for decision in report.session.risk_decisions
        if decision.approved
    }
    assert approved_ids
    assert {intent.order_id for intent in report.intents} == approved_ids
    strategies = {intent.strategy_id for intent in report.intents}
    assert strategies == {INSTANCE_ID}


def test_shadow_matches_live_prints_exactly() -> None:
    first = _shadow_report()
    prints = _prints_for(first)
    assert prints

    report = _shadow_shell().run(_ScriptedLiveFeed(_events()), live_prints=prints)

    assert all(
        comparison.verdict is ShadowVerdict.MATCHED for comparison in report.comparisons
    )
    assert report.matched_count == len(report.intents)
    assert report.parity_clean


def test_shadow_detects_price_divergence() -> None:
    prints = _prints_for(_shadow_report())
    divergent = [
        LivePrint(
            order_id=print_.order_id,
            symbol=print_.symbol,
            side=print_.side,
            quantity=print_.quantity,
            price=print_.price + Decimal("0.01"),
            timestamp=print_.timestamp,
        )
        for print_ in prints
    ]

    report = _shadow_shell().run(_ScriptedLiveFeed(_events()), live_prints=divergent)

    assert report.diverged_count == len(report.intents)
    assert not report.parity_clean
    details = [comparison.detail for comparison in report.comparisons]
    assert all(detail is not None and "price" in detail for detail in details)
    assert report.summary().startswith("shadow parity diverged")


def test_shadow_flags_live_prints_without_intents() -> None:
    ghost = LivePrint(
        order_id="ghost-1",
        symbol="SPY",
        side=Side.SELL,
        quantity=Decimal("7"),
        price=Decimal("450.00"),
        timestamp=OPEN,
    )

    report = _shadow_shell().run(_ScriptedLiveFeed(_events()), live_prints=[ghost])

    assert report.live_only_count == 1
    assert report.live_only_prints == (ghost,)
    assert not report.parity_clean


def _resting_limit_config() -> TraderConfig:
    return _example_config().model_copy(
        update={
            "mode": "shadow",
            "seed": 7,
            "strategy_instances": (
                StrategyInstanceConfig(
                    instance_id=INSTANCE_ID,
                    implementation=f"{STRATEGY_MODULE}:RestingLimitStrategy",
                    bundle_hash=BUNDLE,
                    capital_fraction=Decimal("1.0"),
                    symbols=("SPY",),
                ),
            ),
            "risk_pipeline": RISK,
            "fees": FEES,
        }
    )


def test_unfilled_resting_limit_intent_is_reported() -> None:
    shell = ShadowShell(_resting_limit_config(), options=_options(run_id="shadow-rest"))
    report = shell.run(_ScriptedLiveFeed(_events()))

    assert report.session.order_count == 1
    assert len(report.intents) == 1
    assert report.session.fills == ()
    comparison = report.comparisons[0]
    assert comparison.verdict is ShadowVerdict.UNFILLED
    assert comparison.would_be == ()
    assert comparison.live is None
    assert report.unfilled_count == 1
    assert report.parity_clean


def test_unfilled_intent_with_live_print_diverges() -> None:
    shell = ShadowShell(_resting_limit_config(), options=_options(run_id="shadow-rest"))
    print_ = LivePrint(
        order_id="rest-1",
        symbol="SPY",
        side=Side.BUY,
        quantity=Decimal("5"),
        price=Decimal("450.01"),
        timestamp=OPEN,
    )
    report = shell.run(_ScriptedLiveFeed(_events()), live_prints=[print_])

    comparison = report.comparisons[0]
    assert comparison.verdict is ShadowVerdict.DIVERGED
    assert comparison.detail == "no simulated fill; live printed"
    assert comparison.live == print_
    assert not report.parity_clean


def test_shadow_runs_a_live_boundary_until_stop() -> None:
    feed = _IdlingLiveFeed(_events())
    holder: dict[str, ShadowShell] = {}
    polls = {"count": 0}

    def pace() -> None:
        polls["count"] += 1
        if polls["count"] >= 3:
            holder["shell"].stop("idle budget exhausted")

    shell = _shadow_shell(options=_options(idle_pace=pace))
    holder["shell"] = shell
    report = shell.run(feed)

    assert polls["count"] == 3
    assert feed.closed
    assert report.session.status is PaperSessionStatus.STOPPED
    assert report.session.reason == "idle budget exhausted"
    assert len(report.intents) == report.session.order_count


def test_shadow_entrypoint_composes_one_session() -> None:
    report = run_shadow_session(
        _trader_config(),
        _ScriptedLiveFeed(_events()),
        options=_options(run_id="shadow-entry"),
    )

    assert report.run_id == "shadow-entry"
    assert report.session.status is PaperSessionStatus.COMPLETED
    assert report.parity_clean


def test_shadow_artifacts_are_deterministic() -> None:
    first = _shadow_report()
    second = _shadow_report()

    left = first.artifacts()
    right = second.artifacts()
    assert left == right
    assert json.dumps(left, sort_keys=True)
    session_artifacts = cast("dict[str, object]", left["session"])
    counts = cast("dict[str, object]", left["counts"])
    assert "booted_at" not in left
    assert "ended_at" not in left
    assert "booted_at" not in session_artifacts
    assert "ended_at" not in session_artifacts
    assert counts["intents"] == len(first.intents)
    assert left["parity_clean"] is True
    assert left["schema"] == "fca-shadow-report-v1"


def test_live_prints_require_unique_order_ids() -> None:
    duplicate = LivePrint(
        order_id="dupe",
        symbol="SPY",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("450.00"),
        timestamp=OPEN,
    )

    with pytest.raises(ShadowConfigurationError, match="duplicate"):
        _shadow_shell().run(
            _ScriptedLiveFeed(_events()), live_prints=[duplicate, duplicate]
        )


def test_shadow_rejects_live_mode_configuration() -> None:
    with pytest.raises(ShadowConfigurationError, match="paper or shadow"):
        _shadow_shell(mode="live")


def test_shadow_validates_feed_and_options_types() -> None:
    shell = _shadow_shell()
    with pytest.raises(ShadowConfigurationError, match="PaperFeed"):
        shell.run(cast("PaperFeed", _events()))
    with pytest.raises(ShadowConfigurationError, match="TraderConfig"):
        ShadowShell(cast("TraderConfig", object()), options=_options())
    with pytest.raises(ShadowConfigurationError, match="PaperOptions"):
        ShadowShell(_trader_config(), options=cast("PaperOptions", object()))
    with pytest.raises(ShadowConfigurationError, match="sink must be"):
        ShadowShell(
            _trader_config(),
            options=_options(),
            sink=cast("ObserveOnlyBrokerSink", object()),
        )
