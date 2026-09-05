"""GoLiveGate tests: criteria, state machine, refusals, deployment validation."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from harness.configuration import (
    RiskPipelineConfig,
    RiskRuleConfig,
    StrategyInstanceConfig,
    TraderConfig,
    canonical_hash,
    load_example,
)
from harness.core.symbols import Symbol
from harness.core.types import (
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.data import LakeReplayFeed, ReplayStream
from harness.risk import RiskAction, RuleDecision
from harness.runtime.go_live import (
    GateState,
    GoLiveGate,
    GoLiveRefused,
    PaperSessionEvidence,
    validate_live_deployment,
)
from harness.runtime.paper import (
    PaperOptions,
    PaperSessionPolicy,
    PaperSessionStatus,
    ReplayPaperFeed,
    run_paper_session,
)
from harness.strategy import Strategy, StrategyContext
from harness.testing.paper_parity import (
    PaperParityScenario,
    ParityDimension,
    ParityDivergence,
    run_paper_parity,
)
from lake.contracts import ExchangeSession

STRATEGY_MODULE = "fca_go_live_test_strategies"
BUNDLE = "c" * 64
RUN_ID = "gate-paper-1"
CASH = Decimal("100000")
INSTANCE_ID = "solo"
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
QUOTES = 4

_TRADER_EXAMPLE = load_example("trader")
assert isinstance(_TRADER_EXAMPLE, TraderConfig)
FEES = _TRADER_EXAMPLE.fees
RISK = RiskPipelineConfig(
    rules=(
        RiskRuleConfig(
            rule_id="gate-max-order",
            implementation="harness.risk.limits:MaxOrderNotional",
            parameters={"limit": Decimal("1000000")},
        ),
    ),
    on_reject="reject_order",
)
RISK_HASH = canonical_hash(RISK)
FEE_HASH = canonical_hash(FEES)
CRITERIA_NAMES = (
    "clean_session_streak",
    "parity_evidence",
    "universe_parity",
    "risk_parity",
    "risk_veto_sanity",
)


# --------------------------------------------------------------------------
# Test strategies and rules
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
        order = SingleLegOrder(
            event_id=f"{self.prefix}-{self.quotes}",
            timestamp=event.timestamp,
            sequence=event.sequence,
            order_id=f"{self.prefix}-{self.quotes}",
            symbol=event.symbol,
            side=Side.BUY,
            quantity=self.quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
        )
        self.context.orders.submit(order)


class AbortingStrategy(Strategy):
    """Raise :class:`BacktestAbort` after a deterministic number of quotes."""

    def __init__(self, **parameters: object) -> None:
        self.abort_at = int(str(parameters.get("abort_at", "2")))
        self.quotes = 0

    def on_quote(self, event: QuoteEvent) -> None:
        from harness.runtime.backtest import BacktestAbort

        self.quotes += 1
        if self.quotes >= self.abort_at:
            raise BacktestAbort(f"abort after {self.quotes} quotes")


class SilentVetoRule:
    """Veto every order without an explicit reason, breaking veto sanity."""

    def __init__(
        self,
        parameters: dict[str, object] | None = None,
        *,
        rule_id: str = "silent-veto",
    ) -> None:
        self.rule_id = rule_id

    def evaluate(self, order: Any, context: Any) -> RuleDecision:
        return RuleDecision(RiskAction.REJECT, order)


def _register_strategy_module() -> None:
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (
        ("PeriodicMarketStrategy", PeriodicMarketStrategy),
        ("AbortingStrategy", AbortingStrategy),
        ("SilentVetoRule", SilentVetoRule),
    ):
        setattr(module, name, value)
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


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


def _instance(
    implementation: str = f"{STRATEGY_MODULE}:PeriodicMarketStrategy",
    parameters: dict[str, Any] | None = None,
    *,
    symbols: tuple[str, ...] = ("SPY",),
) -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=INSTANCE_ID,
        implementation=implementation,
        bundle_hash=BUNDLE,
        capital_fraction=Decimal("1.0"),
        symbols=symbols,
        parameters={} if parameters is None else parameters,
    )


def _example_config() -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    return example


def _trader_config(mode: str = "paper", **overrides: object) -> TraderConfig:
    updates: dict[str, object] = {
        "mode": mode,
        "seed": 7,
        "strategy_instances": (_instance(),),
        "risk_pipeline": RISK,
        "fees": FEES,
    }
    updates.update(overrides)
    return _example_config().model_copy(update=updates)


def _universe(configuration: TraderConfig) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(symbol)
                for instance in configuration.strategy_instances
                for symbol in instance.symbols
            }
        )
    )


def _evidence(
    run_id: str = "paper-1",
    *,
    clean: bool = True,
    universe: tuple[str, ...] = ("SPY",),
    risk_hash: str | None = None,
    fee_hash: str | None = None,
    unclean_reason: str | None = None,
    veto_count: int = 0,
    unexplained_vetoes: int = 0,
    runtime_halt_vetoes: int = 0,
    veto_reasons: tuple[str, ...] = (),
) -> PaperSessionEvidence:
    if not clean and unclean_reason is None:
        unclean_reason = "session aborted: injected"
    return PaperSessionEvidence(
        run_id=run_id,
        clean=clean,
        universe=universe,
        risk_pipeline_hash=RISK_HASH if risk_hash is None else risk_hash,
        fee_schedule_hash=FEE_HASH if fee_hash is None else fee_hash,
        unclean_reason=unclean_reason,
        veto_count=veto_count,
        unexplained_vetoes=unexplained_vetoes,
        runtime_halt_vetoes=runtime_halt_vetoes,
        veto_reasons=veto_reasons,
    )


def _record_sessions(gate: GoLiveGate, count: int, **overrides: object) -> None:
    for _ in range(count):
        run_id = f"paper-{len(gate.sessions) + 1}"
        gate.record_paper_session(_evidence(run_id, **cast("Any", overrides)))


def _parity_scenario() -> PaperParityScenario:
    return PaperParityScenario(
        configuration=_trader_config(),
        events=_events(),
        run_id="gate-parity",
        initial_cash=CASH,
    )


def _parity_report() -> Any:
    return run_paper_parity(_parity_scenario())


def _eligible_gate(**gate_kwargs: object) -> GoLiveGate:
    gate = GoLiveGate(**cast("Any", gate_kwargs))
    candidate = gate_kwargs.get("live_configuration")
    live = candidate if isinstance(candidate, TraderConfig) else None
    universe = ("SPY",) if live is None else _universe(live)
    risk_hash = RISK_HASH if live is None else canonical_hash(live.risk_pipeline)
    fee_hash = FEE_HASH if live is None else canonical_hash(live.fees)
    _record_sessions(
        gate, 10, universe=universe, risk_hash=risk_hash, fee_hash=fee_hash
    )
    gate.record_parity_report(_parity_report())
    decision = gate.evaluate()
    assert decision.eligible
    return gate


def _run_paper(configuration: TraderConfig) -> Any:
    feed = LakeReplayFeed(
        (ExchangeSession(OPEN.date(), OPEN, OPEN + timedelta(days=1)),),
        streams=(ReplayStream("SPY", None, tuple(_events())),),
    )
    options = PaperOptions(
        run_id=RUN_ID,
        initial_cash=CASH,
        session_policy=PaperSessionPolicy.OPEN_ENDED,
        start=OPEN,
    )
    return run_paper_session(configuration, ReplayPaperFeed(feed), options=options)


def _criterion(decision: Any, name: str) -> Any:
    return next(criterion for criterion in decision.criteria if criterion.name == name)


# --------------------------------------------------------------------------
# Criteria and refusals
# --------------------------------------------------------------------------


def test_fresh_gate_refuses_every_criterion() -> None:
    gate = GoLiveGate()
    decision = gate.evaluate()

    assert gate.state is GateState.NOT_ELIGIBLE
    assert not decision.eligible
    assert decision.unmet == CRITERIA_NAMES
    assert len(decision.refusals) == len(CRITERIA_NAMES)
    for criterion in decision.criteria:
        assert not criterion.satisfied
        assert criterion.detail
        assert criterion.evidence
    assert json.dumps(decision.artifacts(), sort_keys=True)
    assert gate.transitions == ()
    with pytest.raises(GoLiveRefused) as excinfo:
        decision.require_eligible()
    assert len(excinfo.value.refusals) == len(CRITERIA_NAMES)
    assert excinfo.value.decision is decision
    assert str(excinfo.value).startswith("go-live refused")


def test_ten_clean_sessions_and_parity_become_eligible() -> None:
    gate = GoLiveGate()
    gate.record_paper_session(_evidence("paper-1"))
    accumulating = gate.evaluate()
    assert accumulating.state is GateState.ACCUMULATING
    assert not accumulating.eligible

    _record_sessions(gate, 9)
    gate.record_parity_report(_parity_report())
    decision = gate.evaluate()

    assert decision.eligible
    assert decision.state is GateState.ELIGIBLE
    assert decision.refusals == ()
    assert decision.unmet == ()
    streak = _criterion(decision, "clean_session_streak")
    assert streak.satisfied
    assert streak.evidence["streak"] == 10
    assert streak.evidence["required"] == 10
    returned = gate.require_eligible()
    assert returned.eligible
    assert [(t.from_state, t.to_state) for t in gate.transitions] == [
        (GateState.NOT_ELIGIBLE, GateState.ACCUMULATING),
        (GateState.ACCUMULATING, GateState.ELIGIBLE),
    ]


def test_dirty_session_resets_the_clean_streak() -> None:
    gate = GoLiveGate()
    _record_sessions(gate, 3)
    gate.record_paper_session(
        _evidence("dirty-1", clean=False, unclean_reason="session aborted: injected")
    )
    _record_sessions(gate, 2)

    decision = gate.evaluate()

    streak = _criterion(decision, "clean_session_streak")
    assert not streak.satisfied
    assert streak.evidence["streak"] == 2
    assert not decision.eligible
    assert any("clean_session_streak" in refusal for refusal in decision.refusals)
    assert gate.state is GateState.ACCUMULATING


def test_parity_evidence_requires_a_clean_latest_report() -> None:
    gate = GoLiveGate()
    _record_sessions(gate, 10)
    decision = gate.evaluate()

    assert "parity_evidence" in decision.unmet

    gate.record_parity_report(_parity_report())
    assert gate.evaluate().eligible

    diverged_gate = GoLiveGate()
    _record_sessions(diverged_gate, 10)
    diverged = replace(
        _parity_report(),
        dimensions=(
            ParityDimension(
                ParityDivergence.DISPATCH.value, False, "injected divergence"
            ),
        ),
    )
    diverged_gate.record_parity_report(diverged)
    diverged_decision = diverged_gate.evaluate()

    assert not diverged_decision.eligible
    criterion = _criterion(diverged_decision, "parity_evidence")
    assert not criterion.satisfied
    assert "dispatch" in criterion.detail
    assert criterion.evidence["reports"][0]["divergences"] == ["dispatch"]


def test_universe_parity_enforced_across_sessions_and_candidate() -> None:
    mixed = GoLiveGate()
    _record_sessions(mixed, 5)
    _record_sessions(mixed, 5, universe=("QQQ", "SPY"))
    decision = mixed.evaluate()

    assert not decision.eligible
    assert "universe_parity" in decision.unmet

    bound = GoLiveGate(live_configuration=_trader_config(mode="live"))
    _record_sessions(bound, 10, universe=("QQQ",))
    bound_decision = bound.evaluate()

    assert "universe_parity" in bound_decision.unmet
    matching = GoLiveGate(live_configuration=_trader_config(mode="live"))
    _record_sessions(matching, 10)
    assert "universe_parity" not in matching.evaluate().unmet


def test_risk_parity_enforced_across_sessions_and_candidate() -> None:
    drifted = GoLiveGate()
    _record_sessions(drifted, 9)
    _record_sessions(drifted, 1, fee_hash="f" * 64)
    decision = drifted.evaluate()

    assert not decision.eligible
    assert "risk_parity" in decision.unmet

    candidate = _trader_config(mode="live")
    bound = GoLiveGate(live_configuration=candidate)
    _record_sessions(bound, 10, fee_hash="e" * 64)

    assert "risk_parity" in bound.evaluate().unmet

    matching = GoLiveGate(live_configuration=candidate)
    _record_sessions(
        matching,
        10,
        risk_hash=canonical_hash(candidate.risk_pipeline),
        fee_hash=canonical_hash(candidate.fees),
    )

    assert "risk_parity" not in matching.evaluate().unmet


def test_risk_veto_sanity_enforced() -> None:
    explained = GoLiveGate()
    _record_sessions(explained, 2, veto_count=1, veto_reasons=("max notional",))
    assert "risk_veto_sanity" not in explained.evaluate().unmet

    unexplained = GoLiveGate()
    _record_sessions(
        unexplained,
        2,
        veto_count=2,
        unexplained_vetoes=1,
        veto_reasons=("max notional",),
    )
    decision = unexplained.evaluate()

    assert not decision.eligible
    criterion = _criterion(decision, "risk_veto_sanity")
    assert "without an explicit reason" in criterion.detail

    runtime_halt = GoLiveGate()
    _record_sessions(
        runtime_halt,
        2,
        veto_count=1,
        runtime_halt_vetoes=1,
        veto_reasons=("runtime halt",),
    )
    halt_decision = runtime_halt.evaluate()

    assert "risk_veto_sanity" in halt_decision.unmet
    assert "runtime halt" in _criterion(halt_decision, "risk_veto_sanity").detail


# --------------------------------------------------------------------------
# State machine and evidence composition
# --------------------------------------------------------------------------


def test_state_transitions_are_audited() -> None:
    gate = GoLiveGate()
    gate.evaluate()
    assert gate.transitions == ()

    gate.record_paper_session(_evidence("paper-1"))
    gate.evaluate()
    first = gate.transitions[0]
    assert first.sequence == 1
    assert first.from_state is GateState.NOT_ELIGIBLE
    assert first.to_state is GateState.ACCUMULATING

    _record_sessions(gate, 9)
    gate.record_parity_report(_parity_report())
    eligible = gate.evaluate()
    assert eligible.eligible
    second = gate.transitions[1]
    assert second.sequence == 2
    assert second.to_state is GateState.ELIGIBLE

    gate.evaluate()
    assert len(gate.transitions) == 2
    assert [d.sequence for d in gate.decisions] == [1, 2, 3, 4]


def test_gate_composes_the_parity_harness() -> None:
    gate = GoLiveGate()
    report = gate.record_parity(_parity_scenario())

    assert report.parity_clean
    assert gate.parity_reports == (report,)
    decision = gate.evaluate()
    assert _criterion(decision, "parity_evidence").satisfied


def test_evidence_from_real_paper_sessions() -> None:
    config = _trader_config()
    result = _run_paper(config)
    evidence = PaperSessionEvidence.from_paper_result(result, config)

    assert evidence.clean
    assert evidence.unclean_reason is None
    assert evidence.run_id == RUN_ID
    assert evidence.universe == ("SPY",)
    assert evidence.risk_pipeline_hash == canonical_hash(config.risk_pipeline)
    assert evidence.fee_schedule_hash == canonical_hash(config.fees)
    assert evidence.veto_count == 0
    assert result.status is PaperSessionStatus.COMPLETED

    aborting = _trader_config(
        strategy_instances=(
            _instance(f"{STRATEGY_MODULE}:AbortingStrategy", {"abort_at": "2"}),
        )
    )
    aborted = PaperSessionEvidence.from_paper_result(_run_paper(aborting), aborting)

    assert not aborted.clean
    assert aborted.unclean_reason is not None
    assert "aborted" in aborted.unclean_reason

    silent = RiskPipelineConfig(
        rules=(
            RiskRuleConfig(
                rule_id="silent-veto",
                implementation=f"{STRATEGY_MODULE}:SilentVetoRule",
            ),
        ),
        on_reject="reject_order",
    )
    vetoed_config = _trader_config(risk_pipeline=silent)
    vetoed = PaperSessionEvidence.from_paper_result(
        _run_paper(vetoed_config), vetoed_config
    )

    assert vetoed.clean
    assert vetoed.veto_count > 0
    assert vetoed.unexplained_vetoes == vetoed.veto_count

    gate = GoLiveGate()
    gate.record_paper_session(vetoed)
    gate.record_paper_session(_evidence("paper-2"))
    gate.record_parity_report(_parity_report())
    decision = gate.evaluate()

    assert not decision.eligible
    assert "risk_veto_sanity" in decision.unmet


# --------------------------------------------------------------------------
# Deployment validation
# --------------------------------------------------------------------------


def test_validate_live_deployment_rejects_ineligible_gates() -> None:
    with pytest.raises(GoLiveRefused) as excinfo:
        validate_live_deployment(GoLiveGate(), _trader_config(mode="live"))

    messages = " | ".join(excinfo.value.refusals)
    assert "clean_session_streak" in messages
    assert excinfo.value.decision is not None


def test_validate_live_deployment_requires_live_mode() -> None:
    gate = _eligible_gate()
    with pytest.raises(GoLiveRefused, match="mode 'live'"):
        validate_live_deployment(gate, _trader_config(mode="paper"))


def test_validate_live_deployment_rejects_universe_drift() -> None:
    gate = _eligible_gate()
    drifted = _trader_config(
        mode="live",
        strategy_instances=(_instance(symbols=("QQQ",)),),
    )
    with pytest.raises(GoLiveRefused, match="universe"):
        validate_live_deployment(gate, drifted)


def test_validate_live_deployment_rejects_risk_drift() -> None:
    gate = _eligible_gate()
    drifted = _trader_config(
        mode="live",
        risk_pipeline=RISK.model_copy(
            update={
                "rules": (
                    RiskRuleConfig(
                        rule_id="gate-max-order",
                        implementation="harness.risk.limits:MaxOrderNotional",
                        parameters={"limit": Decimal("1")},
                    ),
                )
            }
        ),
    )
    with pytest.raises(GoLiveRefused, match="risk pipeline or fee schedule"):
        validate_live_deployment(gate, drifted)


def test_validate_live_deployment_rejects_bound_configuration_drift() -> None:
    gate = _eligible_gate(live_configuration=_trader_config(mode="live"))
    with pytest.raises(GoLiveRefused, match="evaluated"):
        validate_live_deployment(gate, _trader_config(mode="live", seed=99))


def test_validate_live_deployment_accepts_eligible_bound_gate() -> None:
    live = _trader_config(mode="live")
    gate = _eligible_gate(live_configuration=live)

    decision = validate_live_deployment(gate, live)

    assert decision.eligible
    assert decision.state is GateState.ELIGIBLE


def test_gate_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        GoLiveGate(required_clean_sessions=0)
    with pytest.raises(ValueError):
        GoLiveGate(required_clean_sessions=True)
    with pytest.raises(ValueError):
        GoLiveGate(live_configuration=cast("TraderConfig", object()))


def test_session_evidence_validates_its_records() -> None:
    with pytest.raises(ValueError, match="unclean_reason"):
        PaperSessionEvidence(
            run_id="paper-x",
            clean=False,
            universe=("SPY",),
            risk_pipeline_hash=RISK_HASH,
            fee_schedule_hash=FEE_HASH,
        )
    with pytest.raises(ValueError, match="sorted"):
        _evidence(universe=("SPY", "QQQ", "AAPL"))
    with pytest.raises(ValueError, match="unique"):
        _evidence(universe=("SPY", "SPY"))
    with pytest.raises(ValueError, match="exceed"):
        _evidence(unexplained_vetoes=2, veto_count=1)
    with pytest.raises(ValueError, match="veto_reasons"):
        _evidence(veto_count=2, veto_reasons=("only-one",))
    with pytest.raises(ValueError, match="non-empty"):
        _evidence(universe=())
