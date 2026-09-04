"""PaperShell behavior tests: session policies, ledgers, recorder, validation."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from harness.configuration import (
    BacktestConfig,
    RiskPipelineConfig,
    RiskRuleConfig,
    StrategyInstanceConfig,
    TraderConfig,
    load_example,
)
from harness.core.clock import SimClock
from harness.core.symbols import Symbol
from harness.core.types import (
    Fill,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
)
from harness.data import LakeReplayFeed, ReplayStream
from harness.data.live_recorder import (
    LiveRecorder,
    LiveRecorderConfig,
    LocalLiveObjectStore,
)
from harness.risk import RiskAction, RiskPipeline, RuleDecision
from harness.runtime.backtest import (
    AbortPolicy,
    BacktestAborted,
    BacktestOptions,
    FillModel,
    run_backtest,
)
from harness.runtime.paper import (
    InboxPaperFeed,
    PaperConfigurationError,
    PaperOptions,
    PaperSessionPolicy,
    PaperSessionStatus,
    PaperShell,
    PaperShellError,
    ReplayPaperFeed,
)
from harness.strategy import Strategy, StrategyContext
from lake.contracts import ExchangeSession

STRATEGY_MODULE = "fca_paper_test_strategies"
BUNDLE = "b" * 64
RUN_ID = "paper-session-1"
CASH = Decimal("100000")
INSTANCE_ID = "solo"
SESSION_DATE = date(2024, 1, 2)
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
CLOSE = datetime(2024, 1, 2, 16, 30, tzinfo=UTC)
BACKTEST_END = datetime(2024, 1, 2, 20, 0, tzinfo=UTC)

_TRADER_EXAMPLE = load_example("trader")
assert isinstance(_TRADER_EXAMPLE, TraderConfig)
FEES = _TRADER_EXAMPLE.fees
RISK = RiskPipelineConfig(
    rules=(
        RiskRuleConfig(
            rule_id="paper-max-order",
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
        from harness.runtime.backtest import StrategyHalt

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
        from harness.runtime.backtest import BacktestAbort

        self.quotes += 1
        if self.quotes >= self.abort_at:
            raise BacktestAbort(f"abort after {self.quotes} quotes")

    def on_stop(self, context: StrategyContext) -> None:
        self.stopped = True


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


class _MockLiveInbox:
    """Mock-live boundary: frames pushed by a deployment, popped by the shell."""

    __slots__ = ("_frames",)

    def __init__(self) -> None:
        self._frames: list[QuoteEvent | TradeEvent] = []

    def push(self, events: Iterable[QuoteEvent | TradeEvent]) -> None:
        self._frames.extend(events)

    def pop(self) -> QuoteEvent | TradeEvent | None:
        return self._frames.pop(0) if self._frames else None

    def next_event(self) -> QuoteEvent | TradeEvent | None:
        return self.pop()

    @property
    def exhausted(self) -> bool:
        return not self._frames

    def close(self) -> None:
        return None


class _FrozenClock:
    """Deterministic wall-clock adapter returning one fixed instant."""

    __slots__ = ("_moment",)

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


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


def _quote(moment: datetime, sequence: int) -> QuoteEvent:
    return QuoteEvent(
        event_id=f"quote-{moment.isoformat(timespec='microseconds')}",
        timestamp=moment,
        sequence=sequence,
        symbol=Symbol("SPY"),
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


def _backtest_config(**overrides: object) -> BacktestConfig:
    example = load_example("backtest")
    assert isinstance(example, BacktestConfig)
    updates: dict[str, object] = {
        "run_id": RUN_ID,
        "start": OPEN,
        "end": BACKTEST_END,
        "seed": 7,
        "initial_cash": CASH,
        "strategy_instances": (_instance(),),
        "risk_pipeline": RISK,
        "fees": FEES,
    }
    updates.update(overrides)
    return example.model_copy(update=updates)


def _options(**overrides: object) -> PaperOptions:
    values: dict[str, object] = {"run_id": RUN_ID, "initial_cash": CASH}
    values.update(overrides)
    return PaperOptions(**cast(dict[str, Any], values))


def _shell(**overrides: object) -> PaperShell:
    return PaperShell(_trader_config(), options=_options(**overrides))


def _replay_feed(events: Iterable[QuoteEvent | TradeEvent]) -> ReplayPaperFeed:
    return ReplayPaperFeed(
        LakeReplayFeed(
            sessions=(ExchangeSession(SESSION_DATE, OPEN, BACKTEST_END),),
            streams={"SPY": list(events)},
        )
    )


# --------------------------------------------------------------------------
# Mock-live session, books, recorder, and artifacts
# --------------------------------------------------------------------------


def test_three_hour_mock_live_session_exits_cleanly(tmp_path) -> None:
    inbox = _MockLiveInbox()
    events = _events()
    inbox.push(events)
    recorder = LiveRecorder(
        LiveRecorderConfig(
            deployment="paper-test",
            asset_class="equity",
            store=LocalLiveObjectStore(tmp_path),
            clock=SimClock(OPEN),
            batch_size=1,
        )
    )
    shell = PaperShell(
        _trader_config(),
        options=_options(
            session_policy=PaperSessionPolicy.STOP_AT_CLOSE,
            session=ExchangeSession(SESSION_DATE, OPEN, CLOSE),
        ),
        recorder=recorder,
        wall_clock=_FrozenClock(OPEN),
    )

    result = shell.run(InboxPaperFeed(inbox))

    assert result.status is PaperSessionStatus.AT_CLOSE
    assert result.reason is not None and "closed at" in result.reason
    assert result.booted_at == OPEN
    assert result.ended_at == OPEN
    assert result.last_event_time == CLOSE - timedelta(minutes=3)
    assert result.market_event_count == 80
    assert result.out_of_session_events == 0
    assert result.fills
    snapshot = result.portfolio
    assert tuple(book.book_id for book in snapshot.books) == (INSTANCE_ID,)
    book = snapshot.book(INSTANCE_ID)
    assert book.fill_count == len(result.fills)
    assert book.cash.is_finite()
    assert snapshot.equity is not None
    assert result.recorded_rows == {"quote/SPY": 60, "trade/SPY": 20}
    assert recorder.closed
    parts = list(tmp_path.rglob("*.parquet"))
    assert len(parts) == 80
    assert any("date=2024-01-02" in str(part) for part in parts)
    artifacts = json.loads(json.dumps(result.artifacts()))
    assert artifacts["status"] == "at_close"
    assert artifacts["counts"]["market_events"] == 80
    assert artifacts["recorded_rows"] == {"quote/SPY": 60, "trade/SPY": 20}


def test_paper_ledger_matches_backtest_ledger() -> None:
    events = _events()
    backtest = run_backtest(
        _backtest_config(),
        data_source={"SPY": ReplayStream("SPY", None, list(events))},
        options=BacktestOptions(fill_model=FillModel.SIMPLE_TOUCH),
    )
    shell = PaperShell(_trader_config(), options=_options(start=OPEN))

    paper = shell.run(_replay_feed(events))

    assert paper.status is PaperSessionStatus.COMPLETED
    assert paper.market_event_count == backtest.market_event_count == len(events)
    assert paper.fills == backtest.fills
    assert paper.fills
    assert paper.fee_assessments == backtest.fee_assessments
    assert paper.portfolio == backtest.portfolio
    assert paper.dispatch_hash == backtest.dispatch_hash
    assert paper.order_count == backtest.order_count > 0


def test_session_window_drops_events_outside_the_window() -> None:
    inbox = _MockLiveInbox()
    events = _events()
    inbox.push(
        (
            _quote(OPEN - timedelta(minutes=6), 0),
            *events[:3],
            _quote(CLOSE + timedelta(minutes=3), 999),
        )
    )
    shell = PaperShell(
        _trader_config(),
        options=_options(
            session_policy=PaperSessionPolicy.SESSION_WINDOW,
            session=ExchangeSession(SESSION_DATE, OPEN, CLOSE),
        ),
    )

    result = shell.run(inbox)

    assert result.status is PaperSessionStatus.COMPLETED
    assert result.out_of_session_events == 2
    assert result.market_event_count == 3
    assert result.last_event_time == events[2].timestamp


def test_open_ended_replay_runs_to_exhaustion() -> None:
    events = _events()
    shell = PaperShell(_trader_config(), options=_options(start=OPEN))

    result = shell.run(_replay_feed(events))

    assert result.completed
    assert result.market_event_count == len(events)
    assert result.last_event_time == events[-1].timestamp
    assert result.feed_fingerprints is not None
    assert result.feed_fingerprints.row


def test_strategy_halt_stops_only_the_strategy() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:HaltingStrategy",
                parameters={"halt_at": "2"},
            ),
        )
    )
    shell = PaperShell(config, options=_options(start=OPEN))

    result = shell.run(_replay_feed(_events()))

    assert result.status is PaperSessionStatus.COMPLETED
    assert result.halted_strategies == (INSTANCE_ID,)
    assert result.stopped_strategies == (INSTANCE_ID,)
    assert result.teardown_errors == ()


def test_strategy_abort_stops_the_session_without_raising() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:AbortingStrategy",
                parameters={"abort_at": "3"},
            ),
        )
    )
    shell = PaperShell(config, options=_options(start=OPEN))

    result = shell.run(_replay_feed(_events()))

    assert result.status is PaperSessionStatus.ABORTED
    assert result.reason is not None and "abort after 3 quotes" in result.reason
    assert result.market_event_count < len(_events())
    assert result.stopped_strategies == (INSTANCE_ID,)
    assert result.teardown_errors == ()


def test_abort_policy_raise_propagates_the_abort_error() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(
                implementation=f"{STRATEGY_MODULE}:AbortingStrategy",
                parameters={"abort_at": "3"},
            ),
        )
    )
    shell = PaperShell(
        config, options=_options(start=OPEN, abort_policy=AbortPolicy.RAISE)
    )

    with pytest.raises(BacktestAborted):
        shell.run(_replay_feed(_events()))

    assert shell.finalized
    assert shell.result().status is PaperSessionStatus.ABORTED


def test_lookahead_violation_aborts_the_session() -> None:
    config = _trader_config(
        strategy_instances=(
            _instance(implementation=f"{STRATEGY_MODULE}:LookaheadStrategy"),
        )
    )
    shell = PaperShell(config, options=_options(start=OPEN))

    result = shell.run(_replay_feed(_events()))

    assert result.status is PaperSessionStatus.ABORTED
    assert result.lookahead_violations == (INSTANCE_ID,)
    assert result.reason is not None and "read data ahead" in result.reason


def test_risk_rejection_records_decisions_and_keeps_books_intact() -> None:
    shell = PaperShell(
        _trader_config(),
        options=_options(start=OPEN),
        risk_pipeline=RiskPipeline((RejectAllRule(),)),
    )

    result = shell.run(_replay_feed(_events()))

    assert result.status is PaperSessionStatus.COMPLETED
    assert result.risk_decisions
    assert all(
        decision.action is RiskAction.REJECT for decision in result.risk_decisions
    )
    assert result.fills == ()
    assert result.portfolio.fill_count == 0
    assert result.portfolio.book(INSTANCE_ID).cash == CASH
    assert result.dispatched_count > result.market_event_count


def test_extra_latency_shifts_fill_timestamps_without_changing_fills() -> None:
    events = _events()
    on_time = PaperShell(_trader_config(), options=_options(start=OPEN)).run(
        _replay_feed(events)
    )
    delayed = PaperShell(
        _trader_config(),
        options=_options(start=OPEN, extra_latency=timedelta(seconds=1)),
    ).run(_replay_feed(events))

    assert on_time.fills and delayed.fills
    assert len(delayed.fills) == len(on_time.fills)
    for base, shifted in zip(on_time.fills, delayed.fills, strict=True):
        assert shifted.timestamp == base.timestamp + timedelta(seconds=1)
        assert (shifted.quantity, shifted.price, shifted.side) == (
            base.quantity,
            base.price,
            base.side,
        )


def test_multi_strategy_routing_attributes_fills_to_books() -> None:
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
    shell = PaperShell(
        _trader_config(strategy_instances=instances), options=_options(start=OPEN)
    )

    result = shell.run(_replay_feed(_events()))

    assert result.completed
    assert tuple(book.book_id for book in result.portfolio.books) == ("alpha", "beta")
    alpha = result.portfolio.book("alpha")
    beta = result.portfolio.book("beta")
    assert alpha.initial_cash == CASH * Decimal("0.6")
    assert beta.initial_cash == CASH * Decimal("0.4")
    assert alpha.fill_count + beta.fill_count == len(result.fills) > 0
    assert {assessment.strategy_id for assessment in result.fee_assessments} == {
        "alpha",
        "beta",
    }
    assert (
        sum(
            1
            for assessment in result.fee_assessments
            if assessment.strategy_id == "alpha"
        )
        == alpha.fill_count
    )


def test_step_driving_and_close_finalize_cleanly() -> None:
    events = _events()
    shell = PaperShell(_trader_config(), options=_options(start=OPEN))

    for event in events[:5]:
        shell.step(event)
    in_flight = shell.result()
    assert in_flight.status is PaperSessionStatus.COMPLETED
    assert in_flight.ended_at is None
    assert in_flight.market_event_count == 5

    shell.stop("manual pause")
    with pytest.raises(PaperShellError):
        shell.step(events[5])

    result = shell.close()

    assert result.status is PaperSessionStatus.STOPPED
    assert result.reason == "manual pause"
    assert result.ended_at is not None
    assert result.market_event_count == 5
    assert shell.finalized
    assert shell.close() is result
    with pytest.raises(PaperShellError):
        shell.run(_replay_feed(events))


def test_feed_boundaries_validate_and_report_exhaustion() -> None:
    events = _events()
    replay = _replay_feed(events)
    assert replay.exhausted is False
    consumed = 0
    while not replay.exhausted:
        if replay.next_event() is not None:
            consumed += 1
    assert consumed == len(events)
    assert replay.exhausted
    with pytest.raises(PaperConfigurationError):
        ReplayPaperFeed(cast(Any, object()))
    with pytest.raises(PaperConfigurationError):
        InboxPaperFeed(cast(Any, object()))

    inbox = _MockLiveInbox()
    inbox.push(events[:2])
    boundary = InboxPaperFeed(inbox)
    assert boundary.exhausted is False
    assert boundary.next_event() is events[0]
    assert boundary.next_event() is events[1]
    assert boundary.next_event() is None
    boundary.close()


def test_shell_rejects_non_trader_and_non_paper_configuration() -> None:
    with pytest.raises(PaperConfigurationError):
        PaperShell(cast(Any, load_example("backtest")), options=_options(start=OPEN))
    with pytest.raises(PaperConfigurationError):
        PaperShell(_trader_config(mode="shadow"), options=_options(start=OPEN))


def test_options_validate_session_and_start_declaration() -> None:
    session = ExchangeSession(SESSION_DATE, OPEN, CLOSE)
    with pytest.raises(PaperConfigurationError):
        PaperOptions(run_id=RUN_ID, initial_cash=CASH)
    with pytest.raises(PaperConfigurationError):
        PaperOptions(run_id=RUN_ID, initial_cash=CASH, session=session)
    with pytest.raises(PaperConfigurationError):
        PaperOptions(
            run_id=RUN_ID,
            initial_cash=CASH,
            session_policy=PaperSessionPolicy.SESSION_WINDOW,
        )
    with pytest.raises(PaperConfigurationError):
        PaperOptions(
            run_id=RUN_ID,
            initial_cash=CASH,
            session_policy=PaperSessionPolicy.STOP_AT_CLOSE,
            session=session,
            start=OPEN,
        )
    with pytest.raises(PaperConfigurationError):
        PaperOptions(run_id=RUN_ID, initial_cash=Decimal("0"), start=OPEN)
    options = PaperOptions(
        run_id=RUN_ID,
        initial_cash=CASH,
        session_policy=PaperSessionPolicy.STOP_AT_CLOSE,
        session=session,
    )
    assert options.effective_start == OPEN
