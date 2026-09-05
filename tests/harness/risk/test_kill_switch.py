"""Global kill switch behavior, audit, halt taxonomy, and PaperShell tests."""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime, timedelta
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
from harness.core.types import QuoteEvent
from harness.core.types.orders import MultiLegOrder, OrderLeg, Side, SingleLegOrder
from harness.data import LakeReplayFeed
from harness.portfolio.hub import AggregatePosition, PortfolioSnapshot
from harness.risk import RiskAction, RiskContext, RiskPipeline, RiskRule
from harness.risk.kill_switch import (
    DEFAULT_KILL_SWITCH_RULE_ID,
    KillSwitch,
    KillSwitchAction,
    KillSwitchEvent,
    KillSwitchState,
)
from harness.risk.limits import MaxOrderNotional, MaxOrderSize
from harness.risk.models import RiskDecision
from harness.runtime.paper import (
    PaperOptions,
    PaperSessionStatus,
    PaperShell,
    PaperShellError,
    ReplayPaperFeed,
)
from harness.strategy import Strategy, StrategyContext
from lake.contracts import ExchangeSession

STAMP = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
ZERO = Decimal("0")

STRATEGY_MODULE = "fca_kill_switch_test_strategies"
BUNDLE = "b" * 64
RUN_ID = "kill-switch-session"
CASH = Decimal("100000")
INSTANCE_ID = "solo"
SESSION_DATE = date(2024, 1, 2)
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
REPLAY_END = datetime(2024, 1, 2, 20, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Risk-rule test helpers
# --------------------------------------------------------------------------


def order(
    quantity: str,
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    order_id: str = "order-1",
) -> SingleLegOrder:
    return SingleLegOrder(
        event_id=f"event-{order_id}",
        timestamp=STAMP,
        sequence=1,
        order_id=order_id,
        symbol=Symbol(symbol),
        side=side,
        quantity=Decimal(quantity),
    )


def multi_leg_order() -> MultiLegOrder:
    return MultiLegOrder(
        event_id="event-multi-1",
        timestamp=STAMP,
        sequence=1,
        order_id="multi-1",
        legs=(
            OrderLeg(symbol=Symbol("AAPL"), side=Side.BUY, quantity=Decimal("5")),
            OrderLeg(symbol=Symbol("MSFT"), side=Side.SELL, quantity=Decimal("3")),
        ),
    )


def position(
    symbol: str,
    quantity: str,
    price: str,
) -> AggregatePosition:
    signed_quantity = Decimal(quantity)
    mark = Decimal(price)
    return AggregatePosition(
        symbol=Symbol(symbol),
        quantity=signed_quantity,
        long_quantity=max(signed_quantity, ZERO),
        short_quantity=max(-signed_quantity, ZERO),
        cost_basis=signed_quantity * mark,
        cash_flow=-signed_quantity * mark,
        average_cost=mark,
        realized_pnl=ZERO,
        mark_price=mark,
        market_value=signed_quantity * mark,
        unrealized_pnl=ZERO,
    )


def context(
    *,
    prices: dict[str, str] | None = None,
) -> RiskContext:
    return RiskContext(
        portfolio=PortfolioSnapshot(
            account_id="kill-switch-account",
            initial_cash=Decimal("100000"),
            cash=Decimal("100000"),
            positions=(),
            books=(),
            fill_count=0,
        ),
        prices={}
        if prices is None
        else {key: Decimal(value) for key, value in prices.items()},
    )


def outcome(
    decision: RiskDecision,
) -> tuple[RiskAction, object, str | None, tuple[str, ...]]:
    """Return the behavior surface that must be golden-identical."""
    return (decision.action, decision.order, decision.reason, decision.reasons)


# --------------------------------------------------------------------------
# Switch state, audit trail, and rule behavior
# --------------------------------------------------------------------------


def test_disengaged_switch_allows_every_order() -> None:
    switch = KillSwitch()
    candidate = order("10")

    single = switch.evaluate(candidate, context(prices={"AAPL": "100"}))
    multi = switch.evaluate(multi_leg_order(), context())

    assert switch.state is KillSwitchState.DISENGAGED
    assert switch.engaged is False
    assert switch.events == ()
    assert single.action is RiskAction.ALLOW
    assert single.halt is False
    assert single.order == candidate
    assert multi.action is RiskAction.ALLOW
    assert multi.order == multi_leg_order()


def test_engaged_switch_rejects_every_order_with_audited_decision() -> None:
    switch = KillSwitch()
    trip = switch.trip(reason="session halted", actor="operator-1")

    single = switch.evaluate(order("10"), context(prices={"AAPL": "100"}))
    multi = switch.evaluate(multi_leg_order(), context())

    assert switch.engaged is True
    assert single.action is RiskAction.REJECT
    assert single.halt is True
    assert single.reason == "global-kill-switch engaged"
    assert single.order == order("10")
    assert multi.action is RiskAction.REJECT
    assert multi.reason == "global-kill-switch engaged"
    metrics = dict(single.metrics)
    assert metrics["switch"] == "global-kill-switch"
    assert metrics["state"] == "engaged"
    assert metrics["trip_sequence"] == trip.sequence
    assert metrics["trip_reason"] == "session halted"
    assert metrics["trip_actor"] == "operator-1"


def test_pipeline_records_the_tripped_switch_in_the_audit_trail() -> None:
    switch = KillSwitch()
    switch.trip(reason="last line of defense", actor="operator-1")
    pipeline = RiskPipeline(
        (switch, MaxOrderSize({"limit": Decimal("5")})),
        on_reject="halt_runtime",
    )

    decision = pipeline.evaluate(order("10"), context())

    assert decision.action is RiskAction.REJECT
    assert decision.halted is True
    assert decision.halt_scope == "halt_runtime"
    assert decision.reason == "global-kill-switch engaged"
    assert decision.reason in decision.veto_reasons
    assert [audit.rule_id for audit in decision.audit_trail] == ["global-kill-switch"]
    audit = decision.audit_trail[0]
    assert audit.action is RiskAction.REJECT
    assert audit.halt is True
    assert audit.reason == "global-kill-switch engaged"
    assert audit.metrics["trip_reason"] == "last line of defense"


def test_trip_and_release_actions_are_explicitly_audited() -> None:
    switch = KillSwitch(rule_id="kill")

    first = switch.trip(reason="trip one", actor="op-1")
    redundant = switch.trip(reason="trip again", actor="op-1")

    assert first == KillSwitchEvent(
        sequence=1,
        action=KillSwitchAction.TRIP,
        reason="trip one",
        actor="op-1",
    )
    assert redundant.sequence == 2
    assert redundant.action is KillSwitchAction.TRIP
    assert switch.engaged is True
    assert switch.last_event == redundant
    release = switch.release(reason="all clear", actor="op-2")
    assert release.sequence == 3
    assert release.action is KillSwitchAction.RELEASE
    assert switch.state is KillSwitchState.DISENGAGED
    assert switch.engaged is False
    assert [event.action for event in switch.events] == [
        KillSwitchAction.TRIP,
        KillSwitchAction.TRIP,
        KillSwitchAction.RELEASE,
    ]
    assert switch.last_event == release
    with pytest.raises(ValueError):
        switch.trip(reason=" ")
    with pytest.raises(ValueError):
        switch.release(actor="")


def test_release_restores_exactly_prior_behavior() -> None:
    switch = KillSwitch(rule_id="kill")
    pipeline = RiskPipeline(
        (switch, MaxOrderSize({"limit": Decimal("5")})),
    )
    before_allow = pipeline.evaluate(order("3"), context())
    before_reduce = pipeline.evaluate(order("10"), context())

    switch.trip(reason="halt", actor="op-1")
    tripped = pipeline.evaluate(order("3"), context())
    switch.release(reason="resume", actor="op-1")
    after_allow = pipeline.evaluate(order("3"), context())
    after_reduce = pipeline.evaluate(order("10"), context())

    assert tripped.action is RiskAction.REJECT
    assert tripped.halted is True
    assert after_allow == before_allow
    assert after_reduce == before_reduce


def test_disengaged_pipeline_is_golden_identical_to_the_rule_free_pipeline() -> None:
    baseline = RiskPipeline((MaxOrderSize({"limit": Decimal("5")}),))
    composed = RiskPipeline((KillSwitch(), MaxOrderSize({"limit": Decimal("5")})))

    for quantity in ("10", "3", "0.5"):
        expected = baseline.evaluate(order(quantity), context())
        actual = composed.evaluate(order(quantity), context())
        assert outcome(actual) == outcome(expected)
        assert actual.approved is expected.approved


def test_halt_taxonomy_scopes_follow_the_pipeline_on_reject_policy() -> None:
    for on_reject, scope in (
        ("reject_order", None),
        ("halt_strategy", "halt_strategy"),
        ("halt_runtime", "halt_runtime"),
    ):
        switch = KillSwitch()
        switch.trip()
        decision = RiskPipeline(
            (switch,),
            on_reject=on_reject,
        ).evaluate(order("1"), context())

        assert decision.rejected is True
        assert decision.halt_scope == scope
        assert decision.halted is True


def test_engaged_decisions_are_deterministic() -> None:
    switch = KillSwitch()
    switch.trip(reason="determinism", actor="op-1")

    first = switch.evaluate(order("1"), context())
    second = switch.evaluate(order("1"), context())

    assert first == second


def test_kill_switch_satisfies_the_risk_rule_contract() -> None:
    switch = KillSwitch()

    assert isinstance(switch, RiskRule)
    assert switch.rule_id == DEFAULT_KILL_SWITCH_RULE_ID
    assert KillSwitch(rule_id="custom").rule_id == "custom"
    with pytest.raises(ValueError):
        KillSwitch(rule_id=" ")
    with pytest.raises(ValueError):
        KillSwitch({"limit": Decimal("1")})


def test_yaml_configuration_builds_the_switch() -> None:
    configuration = RiskPipelineConfig(
        rules=(
            RiskRuleConfig(
                rule_id="kill",
                implementation="harness.risk.kill_switch:KillSwitch",
            ),
        ),
        on_reject="halt_runtime",
    )
    pipeline = RiskPipeline.from_config(configuration)

    switch = cast(KillSwitch, pipeline.rules[0])
    assert switch.rule_id == "kill"
    assert pipeline.evaluate(order("1"), context()).approved is True
    switch.trip(reason="operator kill", actor="op-9")
    decision = pipeline.evaluate(order("1"), context())
    assert decision.rejected is True
    assert decision.halt_scope == "halt_runtime"

    unsupported = RiskPipelineConfig(
        rules=(
            RiskRuleConfig(
                rule_id="kill",
                implementation="harness.risk.kill_switch:KillSwitch",
                parameters={"limit": "1"},
            ),
        ),
        on_reject="reject_order",
    )
    with pytest.raises(ValueError):
        RiskPipeline.from_config(unsupported)


# --------------------------------------------------------------------------
# PaperShell integration
# --------------------------------------------------------------------------


class SubmitOnQuoteStrategy(Strategy):
    """Submit one deterministic market order on every quote."""

    def __init__(self, **parameters: object) -> None:
        self.quantity = Decimal(str(parameters.get("quantity", "10")))
        self.prefix = str(parameters.get("prefix", "order"))
        self.submitted = 0
        self._context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self._context = context

    def on_quote(self, event: QuoteEvent) -> None:
        assert self._context is not None
        self.submitted += 1
        self._context.orders.submit(
            SingleLegOrder(
                event_id=f"{self.prefix}-{self.submitted}",
                timestamp=event.timestamp,
                sequence=event.sequence,
                order_id=f"{self.prefix}-{self.submitted}",
                symbol=event.symbol,
                side=Side.BUY,
                quantity=self.quantity,
            )
        )


def _register_strategy_module() -> None:
    module = types.ModuleType(STRATEGY_MODULE)
    module.__dict__["SubmitOnQuoteStrategy"] = SubmitOnQuoteStrategy
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


def _events(count: int) -> list[QuoteEvent]:
    return [
        QuoteEvent(
            event_id=f"quote-{index}",
            timestamp=OPEN + timedelta(minutes=index),
            sequence=index + 1,
            symbol=Symbol("SPY"),
            bid_price=Decimal("450.00"),
            bid_quantity=Decimal("100"),
            ask_price=Decimal("450.02"),
            ask_quantity=Decimal("100"),
            venue="XNYS",
        )
        for index in range(count)
    ]


def _replay_feed(events: list[QuoteEvent]) -> ReplayPaperFeed:
    return ReplayPaperFeed(
        LakeReplayFeed(
            sessions=(ExchangeSession(SESSION_DATE, OPEN, REPLAY_END),),
            streams={"SPY": list(events)},
        )
    )


def _trader_config() -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    instance = StrategyInstanceConfig(
        instance_id=INSTANCE_ID,
        implementation=f"{STRATEGY_MODULE}:SubmitOnQuoteStrategy",
        bundle_hash=BUNDLE,
        capital_fraction=Decimal("1.0"),
        symbols=("SPY",),
    )
    return example.model_copy(
        update={
            "mode": "paper",
            "seed": 7,
            "strategy_instances": (instance,),
        }
    )


def _options() -> PaperOptions:
    return PaperOptions(run_id=RUN_ID, initial_cash=CASH, start=OPEN)


def _shell(switch: KillSwitch, *, on_reject: str) -> PaperShell:
    return PaperShell(
        _trader_config(),
        options=_options(),
        risk_pipeline=RiskPipeline(
            (
                switch,
                MaxOrderNotional({"limit": Decimal("1000000")}),
            ),
            on_reject=on_reject,
        ),
    )


def test_paper_shell_session_is_golden_identical_when_disengaged() -> None:
    events = _events(12)
    baseline = PaperShell(
        _trader_config(),
        options=_options(),
        risk_pipeline=RiskPipeline((MaxOrderNotional({"limit": Decimal("1000000")}),)),
    )
    switch = KillSwitch()
    composed = _shell(switch, on_reject="reject_order")

    expected = baseline.run(_replay_feed(events))
    actual = composed.run(_replay_feed(events))

    assert expected.status is PaperSessionStatus.COMPLETED
    assert actual.status is PaperSessionStatus.COMPLETED
    assert actual.dispatch_hash == expected.dispatch_hash
    assert actual.fills == expected.fills
    assert actual.fills
    assert actual.fee_assessments == expected.fee_assessments
    assert actual.portfolio == expected.portfolio
    assert actual.order_count == expected.order_count > 0
    assert any(
        audit.rule_id == "global-kill-switch" and audit.action is RiskAction.ALLOW
        for decision in actual.risk_decisions
        for audit in decision.audit_trail
    )
    assert switch.events == ()


def test_tripped_switch_blocks_order_flow_without_halt_scope() -> None:
    switch = KillSwitch()
    switch.trip(reason="end of day", actor="operator-1")
    shell = _shell(switch, on_reject="reject_order")

    result = shell.run(_replay_feed(_events(12)))

    assert result.status is PaperSessionStatus.COMPLETED
    assert result.fills == ()
    assert result.order_count == 0
    assert result.portfolio.book(INSTANCE_ID).cash == CASH
    assert result.risk_decisions
    assert len(result.risk_decisions) == 12
    for decision in result.risk_decisions:
        assert decision.rejected is True
        assert decision.halt_scope is None
        assert decision.halted is True
        assert decision.reason == "global-kill-switch engaged"
    assert result.halted_strategies == ()
    assert len(switch.events) == 1


def test_tripped_switch_halts_the_runtime_under_halt_runtime() -> None:
    switch = KillSwitch()
    switch.trip(reason="manual kill", actor="operator-1")
    shell = _shell(switch, on_reject="halt_runtime")

    result = shell.run(_replay_feed(_events(12)))

    assert result.status is PaperSessionStatus.ABORTED
    assert result.reason is not None
    assert "global-kill-switch engaged" in result.reason
    assert result.fills == ()
    assert result.order_count == 0
    assert result.risk_decisions
    rejected = result.risk_decisions[-1]
    assert rejected.rejected is True
    assert rejected.halt_scope == "halt_runtime"
    assert result.halted_strategies == ()
    assert result.stopped_strategies == (INSTANCE_ID,)


def test_mid_session_trip_halts_dispatch_for_subsequent_steps() -> None:
    switch = KillSwitch()
    shell = _shell(switch, on_reject="halt_runtime")
    events = _events(6)

    for event in events[:3]:
        shell.step(event)
    assert switch.events == ()
    in_flight = shell.result()
    assert in_flight.status is PaperSessionStatus.COMPLETED
    assert in_flight.fills
    assert len(in_flight.risk_decisions) == 3

    trip = switch.trip(reason="manual kill", actor="operator-1")
    shell.step(events[3])

    result = shell.result()
    assert result.status is PaperSessionStatus.ABORTED
    assert result.reason is not None
    assert "global-kill-switch engaged" in result.reason
    assert result.market_event_count == 4
    assert result.fills == in_flight.fills
    rejected = result.risk_decisions[-1]
    assert rejected.rejected is True
    assert rejected.halt_scope == "halt_runtime"
    assert rejected.audit_trail[0].metrics["trip_sequence"] == trip.sequence
    with pytest.raises(PaperShellError):
        shell.step(events[4])
