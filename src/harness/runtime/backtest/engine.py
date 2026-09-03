"""Deterministic backtest engine and the public runner entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

from harness.configuration import BacktestConfig, canonical_hash
from harness.core import EventBus, SimClock
from harness.core.types import (
    Event,
    ExecutionReport,
    ExecutionType,
    Fill,
    OrderRequest,
    OrderStatus,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)
from harness.data import DEFAULT_HISTORY_CAPACITY, HistoricalView, ReplayFingerprints
from harness.fills import (
    AssetClass as FeeAssetClass,
)
from harness.fills import (
    FeeAssessment,
    FeeContext,
    FeeSchedule,
    aggregate_fees,
)
from harness.portfolio import PortfolioHub, VirtualBook
from harness.risk import RiskDecision, RiskPipeline
from harness.runtime.backtest.errors import (
    _ZERO,
    AbortPolicy,
    BacktestAborted,
    BacktestConfigurationError,
    BacktestError,
    BacktestStatus,
    DataPreflightError,
    FillModel,
    LookaheadViolationError,
    StrategyExecutionError,
    StrategyHalt,
    StrategyHaltPolicy,
    StrategyLoadError,
    TeardownPolicy,
    _LoopStop,
)
from harness.runtime.backtest.fees import (
    DEFAULT_FEE_PRECISION,
    fee_schedule_from_config,
)
from harness.runtime.backtest.fills import (
    FillModelOptions,
    FillPlugin,
    build_fill_model,
    fill_model_identity,
    fill_model_latency,
)
from harness.runtime.backtest.fixture import (
    DataPreflight,
    ReplayFixture,
    SyntheticDataOptions,
    _require_backtest_config,
)
from harness.runtime.backtest.manifest import (
    ENGINE_ID,
    ENGINE_VERSION,
    MANIFEST_VERSION,
    BacktestManifest,
    BacktestResult,
    code_fingerprint,
)
from harness.runtime.backtest.preflight import (
    MarketDataSource,
    preflight_data,
    resolve_data_source,
)
from harness.runtime.backtest.strategies import (
    StrategyLoader,
    _GuardedBinding,
    _RiskVenue,
    _strategy_identity,
    _StrategySink,
    default_strategy_loader,
)
from harness.strategy import Strategy, StrategyContext
from harness.venues import SimulatedVenue

UNAtTRIBUTED: Final = "unattributed"


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

_SUBSCRIPTIONS: Final = (
    QuoteEvent,
    TradeEvent,
    ExecutionReport,
    Fill,
)


@dataclass(frozen=True, slots=True)
class BacktestOptions:
    """Explicit, reproducible knobs for one :func:`run_backtest` call."""

    fill_model: FillModel | str = FillModel.SIMPLE_TOUCH
    fill_options: FillModelOptions | None = None
    fee_precision: Decimal = DEFAULT_FEE_PRECISION
    asset_class: FeeAssetClass = FeeAssetClass.EQUITY
    lookahead_guard: bool = True
    history_capacity: int = DEFAULT_HISTORY_CAPACITY
    abort_policy: AbortPolicy = AbortPolicy.RAISE
    strategy_halt_policy: StrategyHaltPolicy = StrategyHaltPolicy.HALT_STRATEGY
    teardown_policy: TeardownPolicy = TeardownPolicy.ALWAYS
    synthetic_options: SyntheticDataOptions | None = None


class _BacktestEngine:
    """Compose one deterministic backtest run from harness capabilities."""

    __slots__ = (
        "_abort_error",
        "_abort_policy",
        "_assessments",
        "_bindings",
        "_bus",
        "_clock",
        "_configuration",
        "_fee_asset_class",
        "_fee_schedule",
        "_fills",
        "_final_fingerprints",
        "_fixture",
        "_halt_policy",
        "_halted",
        "_hub",
        "_last_sequence",
        "_lookahead_guard",
        "_manifest",
        "_market_events",
        "_options",
        "_order_counts",
        "_order_owners",
        "_pipeline",
        "_preflight",
        "_prices",
        "_reject_ordinal",
        "_risk_decisions",
        "_stopped",
        "_teardown_errors",
        "_teardown_policy",
        "_venue",
        "_view",
        "_violations",
    )

    def __init__(
        self,
        configuration: BacktestConfig,
        fixture: ReplayFixture,
        preflight: DataPreflight,
        manifest: BacktestManifest,
        *,
        options: BacktestOptions,
        fee_schedule: FeeSchedule,
        plugin: FillPlugin,
        pipeline: RiskPipeline,
        strategy_loader: StrategyLoader,
    ) -> None:
        self._configuration = configuration
        self._options = options
        self._fee_schedule = fee_schedule
        self._fee_asset_class = options.asset_class
        self._pipeline = pipeline
        self._abort_policy = options.abort_policy
        self._halt_policy = options.strategy_halt_policy
        self._teardown_policy = options.teardown_policy
        self._lookahead_guard = options.lookahead_guard
        self._preflight = preflight
        self._manifest = manifest
        self._fixture = fixture
        self._last_sequence = 0
        self._reject_ordinal = 0
        self._market_events = 0
        self._fills: list[Fill] = []
        self._assessments: list[FeeAssessment] = []
        self._risk_decisions: list[RiskDecision] = []
        self._halted: list[str] = []
        self._stopped: list[str] = []
        self._teardown_errors: list[str] = []
        self._order_counts: dict[str, int] = {}
        self._order_owners: dict[str, str] = {}
        self._prices: dict[str, Decimal] = {}
        self._bindings: list[_GuardedBinding] = []
        self._abort_error: BacktestError | None = None
        self._violations: list[str] = []
        self._final_fingerprints: ReplayFingerprints | None = None

        self._clock = SimClock(configuration.start)
        self._bus = EventBus(self._clock)
        self._hub = PortfolioHub(configuration.run_id)
        for instance in configuration.strategy_instances:
            self._hub.create_book(
                instance.instance_id,
                configuration.initial_cash * instance.capital_fraction,
            )
        self._venue: Any = SimulatedVenue(
            self._bus,
            fill_model=plugin,
            clock=self._clock,
            latency=fill_model_latency(plugin),
        )
        self._view: HistoricalView | None = None
        if options.lookahead_guard:
            view = HistoricalView(
                self._clock,
                capacity=options.history_capacity,
            )
            for symbol in configuration.data.symbols:
                view.subscribe(symbol)
            view.attach(self._bus)
            self._view = view

        self._bus.subscribe(Event, self._observe)
        self._bus.subscribe(QuoteEvent, self._on_market)
        self._bus.subscribe(TradeEvent, self._on_market)
        self._bus.subscribe(Fill, self._on_fill)
        self._bind_strategies(strategy_loader)

    # -- engine state exposed to collaborators -----------------------------

    @property
    def pipeline(self) -> RiskPipeline:
        """Return the configured pre-trade risk pipeline."""
        return self._pipeline

    @property
    def hub(self) -> PortfolioHub:
        """Return the shared portfolio hub."""
        return self._hub

    @property
    def prices(self) -> Mapping[str, Decimal]:
        """Return the latest exact mark per canonical symbol."""
        return self._prices

    def order_count(self, instance_id: str) -> int:
        """Return how many orders one strategy has submitted so far."""
        return self._order_counts.get(instance_id, 0)

    def count_order(self, instance_id: str) -> None:
        """Record one submitted order for a strategy."""
        self._order_counts[instance_id] = self.order_count(instance_id) + 1

    def own_order(self, order_id: str, instance_id: str) -> None:
        """Attribute one order identifier to its submitting strategy."""
        self._order_owners[order_id] = instance_id

    def daily_loss(self, instance_id: str) -> Decimal:
        """Return one strategy's exact loss against its opening allocation."""
        book: VirtualBook = self._hub.book(instance_id)
        equity = book.equity
        if equity is None:
            return _ZERO
        opening = book.initial_cash
        return opening - equity if equity < opening else _ZERO

    def record_risk(self, decision: RiskDecision) -> None:
        """Record one complete risk decision."""
        self._risk_decisions.append(decision)

    def publish_rejection(
        self,
        order: OrderRequest,
        decision: RiskDecision,
        *,
        reason: str | None = None,
    ) -> None:
        """Schedule one rejected execution report for a vetoed order."""
        quantity = _order_quantity(order)
        self._reject_ordinal += 1
        report_id = f"{self._configuration.run_id}-reject-{self._reject_ordinal}"
        text = reason if reason is not None else decision.reason
        self._bus.schedule(
            ExecutionReport(
                event_id=report_id,
                timestamp=self._clock.now(),
                sequence=self._last_sequence + 1,
                report_id=report_id,
                order_id=str(getattr(order, "order_id", "")),
                status=OrderStatus.REJECTED,
                execution_type=ExecutionType.REJECTED,
                order_quantity=quantity,
                filled_quantity=_ZERO,
                remaining_quantity=quantity,
                text=text,
            )
        )

    # -- composition -------------------------------------------------------

    def _bind_strategies(self, strategy_loader: StrategyLoader) -> None:
        for instance in self._configuration.strategy_instances:
            sink = _StrategySink(instance.instance_id)
            strategy_class = strategy_loader(instance)
            if not isinstance(strategy_class, type) or not issubclass(
                strategy_class, Strategy
            ):
                raise StrategyLoadError(
                    f"strategy loader returned an invalid class for "
                    f"{instance.instance_id!r}"
                )
            context = StrategyContext(
                self._clock,
                portfolio=self._hub,
                venue=_RiskVenue(self, instance.instance_id, self._venue),
                bus=self._bus,
                data=self._view,
                seed=self._configuration.seed,
                universe=instance.symbols,
                subscriptions=_SUBSCRIPTIONS,
                book_id=instance.instance_id,
            )
            self._bindings.append(
                _GuardedBinding.bind(
                    strategy_class, dict(instance.parameters), context, sink
                )
            )
        for binding in self._bindings:
            binding.start()

    # -- event loop --------------------------------------------------------

    def run(self) -> BacktestResult:
        """Replay the fixture, settle policies, and build the run result."""
        try:
            self._replay()
        except _LoopStop:
            pass
        finally:
            self._teardown()
        self._raise_abort()
        self._verify_replay()
        return self._result()

    def _replay(self) -> None:
        feed = self._fixture.feed()
        for event in feed:
            self._bus.publish(event)
            self._market_events += 1
            self._drain()
            self._settle()
        self._final_fingerprints = feed.fingerprints

    def _drain(self) -> None:
        while True:
            if self._bus.dispatch_next() is None:
                return
            self._settle()

    def _settle(self) -> None:
        for binding in self._bindings:
            sink = binding.sink
            if sink.abort is not None:
                self._abort(
                    BacktestAborted(
                        f"strategy {sink.instance_id!r} aborted the run: {sink.abort}"
                    )
                )
            elif sink.violation is not None:
                self._lookahead(sink)
            elif sink.error is not None:
                self._strategy_error(sink)
            elif sink.halt is not None:
                self._halt(binding)

    def _abort(self, error: BacktestError) -> None:
        if self._abort_error is None:
            self._abort_error = error
        raise _LoopStop

    def _lookahead(self, sink: _StrategySink) -> None:
        violation = sink.violation
        sink.violation = None
        if sink.instance_id not in self._violations:
            self._violations.append(sink.instance_id)
        self._abort(
            LookaheadViolationError(
                f"strategy {sink.instance_id!r} read data ahead of the engine "
                f"clock: {violation}"
            )
        )

    def _strategy_error(self, sink: _StrategySink) -> None:
        error = sink.error
        sink.error = None
        if error is None:
            return
        raised = StrategyExecutionError(
            f"strategy {sink.instance_id!r} raised {type(error).__name__}: {error}"
        )
        raised.__cause__ = error
        raised.with_traceback(error.__traceback__)
        self._abort(raised)

    def _halt(self, binding: _GuardedBinding) -> None:
        sink = binding.sink
        error = sink.halt
        sink.clear_requests()
        if self._halt_policy is StrategyHaltPolicy.RAISE:
            self._abort(error if error is not None else StrategyHalt("halt requested"))
        if self._halt_policy is StrategyHaltPolicy.ABORT_RUN:
            self._abort(
                BacktestAborted(
                    f"strategy {sink.instance_id!r} halted the run: {error}"
                )
            )
        if sink.instance_id not in self._halted:
            self._halted.append(sink.instance_id)
        self._stop(binding)

    def _stop(self, binding: _GuardedBinding) -> None:
        sink = binding.sink
        binding.stop()
        if sink.stopped and sink.instance_id not in self._stopped:
            self._stopped.append(sink.instance_id)
        if sink.teardown_error is not None and sink.instance_id not in (
            self._teardown_errors
        ):
            self._teardown_errors.append(sink.instance_id)

    def _teardown(self) -> None:
        policy = self._teardown_policy
        if policy is TeardownPolicy.NEVER:
            return
        if policy is TeardownPolicy.ON_SUCCESS and self._abort_error is not None:
            return
        for binding in self._bindings:
            if not binding.started:
                continue
            self._stop(binding)

    def _raise_abort(self) -> None:
        error = self._abort_error
        if error is None or self._abort_policy is not AbortPolicy.RAISE:
            return
        raise error

    # -- bus handlers ------------------------------------------------------

    def _observe(self, event: Event) -> None:
        if event.sequence > self._last_sequence:
            self._last_sequence = event.sequence

    def _on_market(self, event: QuoteEvent | TradeEvent) -> None:
        if isinstance(event, TradeEvent):
            price = event.price
        else:
            price = (event.bid_price + event.ask_price) / 2
        self._prices[str(event.symbol)] = price
        self._hub.mark(event.symbol, price)

    def _on_fill(self, event: Fill) -> None:
        owner = self._order_owners.get(event.order_id)
        strategy_id = UNAtTRIBUTED if owner is None else owner
        assessment = self._fee_schedule.calculate(
            event,
            FeeContext(
                run_id=self._configuration.run_id,
                strategy_id=strategy_id,
                asset_class=self._fee_asset_class,
            ),
        )
        self._assessments.append(assessment)
        booked = replace(event, fee=assessment.total)
        if owner is not None:
            self._hub.apply_fill(owner, booked)
        self._fills.append(booked)

    # -- result ------------------------------------------------------------

    def _result(self) -> BacktestResult:
        status = (
            BacktestStatus.ABORTED
            if self._abort_error is not None
            else BacktestStatus.COMPLETED
        )
        reason = None if self._abort_error is None else str(self._abort_error)
        return BacktestResult(
            configuration=self._configuration,
            manifest=self._manifest,
            status=status,
            reason=reason,
            preflight=self._preflight,
            dispatch_hash=self._bus.dispatch_hash,
            dispatched_count=self._bus.dispatched_count,
            market_event_count=self._market_events,
            order_count=sum(self._order_counts.values()),
            fills=tuple(self._fills),
            fee_assessments=tuple(self._assessments),
            fee_totals=aggregate_fees(self._assessments),
            portfolio=self._hub.snapshot,
            risk_decisions=tuple(self._risk_decisions),
            halted_strategies=tuple(self._halted),
            stopped_strategies=tuple(self._stopped),
            teardown_errors=tuple(self._teardown_errors),
            lookahead_violations=tuple(self._violations),
        )

    def _verify_replay(self) -> None:
        """Confirm the replayed run matches its preflighted fixture exactly.

        An aborted or halted run intentionally stops early, so the row counts
        are only compared for a complete replay.
        """
        if self._fixture.uses_files and self._preflight.file_count == 0:
            raise DataPreflightError("file-backed fixture replayed no partitions")
        if self._abort_error is not None:
            return
        if self._market_events != self._preflight.event_count:
            raise DataPreflightError(
                "replayed event count does not match the preflighted fixture"
            )
        if (
            self._final_fingerprints is not None
            and self._final_fingerprints.data != self._preflight.fingerprints.data
        ):
            raise DataPreflightError(
                "replayed data fingerprint does not match the preflighted fixture"
            )


def _order_quantity(order: OrderRequest) -> Decimal:
    if isinstance(order, SingleLegOrder):
        return order.quantity
    return sum((leg.quantity for leg in order.legs), start=_ZERO)


# --------------------------------------------------------------------------
# Public runner entrypoint
# --------------------------------------------------------------------------


def run_backtest(
    configuration: BacktestConfig,
    *,
    data_source: MarketDataSource | None = None,
    strategy_loader: StrategyLoader | None = None,
    fee_schedule: FeeSchedule | None = None,
    risk_pipeline: RiskPipeline | None = None,
    options: BacktestOptions | None = None,
) -> BacktestResult:
    """Run one deterministic backtest and return its reproducible result.

    ``data_source`` accepts ``None`` for the deterministic synthetic benchmark
    fixture, a :class:`ReplayFixture`, a stream mapping, or a path to a
    user-supplied local Parquet lake directory.  ``options`` carries the fill
    model, fee precision, look-ahead guard, and the abort, halt, and teardown
    policies.

    The runner composes replay, the simulated venue, the risk pipeline, the
    portfolio hub, and the strategies on one :class:`~harness.core.clock.SimClock`
    driven event bus.  Nothing reads wall time, unseeded randomness, or the
    network, and every money or quantity value uses :class:`~decimal.Decimal`.
    """
    _require_backtest_config(configuration)
    chosen = BacktestOptions() if options is None else options
    if not isinstance(chosen, BacktestOptions):
        raise BacktestConfigurationError("options must be a BacktestOptions")
    fixture = resolve_data_source(
        configuration, data_source, options=chosen.synthetic_options
    )
    preflight = preflight_data(configuration, fixture)
    plugin = build_fill_model(chosen.fill_model, chosen.fill_options)
    schedule = (
        fee_schedule_from_config(configuration.fees, precision=chosen.fee_precision)
        if fee_schedule is None
        else fee_schedule
    )
    if not isinstance(schedule, FeeSchedule):
        raise BacktestConfigurationError("fee_schedule must be a FeeSchedule")
    pipeline = (
        RiskPipeline(configuration.risk_pipeline)
        if risk_pipeline is None
        else risk_pipeline
    )
    if not isinstance(pipeline, RiskPipeline):
        raise BacktestConfigurationError("risk_pipeline must be a RiskPipeline")
    loader = default_strategy_loader if strategy_loader is None else strategy_loader
    manifest = build_manifest(
        configuration,
        fixture=fixture,
        preflight=preflight,
        plugin=plugin,
        schedule=schedule,
        pipeline=pipeline,
        options=chosen,
    )
    engine = _BacktestEngine(
        configuration,
        fixture,
        preflight,
        manifest,
        options=chosen,
        fee_schedule=schedule,
        plugin=plugin,
        pipeline=pipeline,
        strategy_loader=loader,
    )
    return engine.run()


def build_manifest(
    configuration: BacktestConfig,
    *,
    fixture: ReplayFixture,
    preflight: DataPreflight,
    plugin: FillPlugin,
    schedule: FeeSchedule,
    pipeline: RiskPipeline,
    options: BacktestOptions | None = None,
) -> BacktestManifest:
    """Build the reproducibility manifest for one planned backtest run."""
    _require_backtest_config(configuration)
    chosen = BacktestOptions() if options is None else options
    identity = fill_model_identity(plugin)
    return BacktestManifest(
        manifest_version=MANIFEST_VERSION,
        engine_id=ENGINE_ID,
        engine_version=ENGINE_VERSION,
        code_fingerprint=code_fingerprint(),
        run_id=configuration.run_id,
        shell=configuration.shell,
        schema_version=configuration.schema_version,
        configuration_hash=canonical_hash(configuration),
        seed=configuration.seed,
        start=configuration.start.isoformat(timespec="microseconds"),
        end=configuration.end.isoformat(timespec="microseconds"),
        base_currency=configuration.base_currency,
        initial_cash=str(configuration.initial_cash),
        data_snapshot_id=configuration.data.snapshot_id,
        data_fingerprint=configuration.data.fingerprint,
        data_source_id=fixture.source_id,
        replay_file_fingerprint=preflight.fingerprints.file,
        replay_row_fingerprint=preflight.fingerprints.row,
        replay_data_fingerprint=preflight.fingerprints.data,
        fill_model=str(identity["model"]),
        fill_model_fingerprint=canonical_hash(identity),
        fee_schedule=schedule.schedule_id,
        fee_fingerprint=canonical_hash(
            {
                "fees": configuration.fees,
                "precision": str(chosen.fee_precision),
                "schedule_id": schedule.schedule_id,
            }
        ),
        risk_pipeline_fingerprint=canonical_hash(
            {
                "pipeline": configuration.risk_pipeline,
                "rules": tuple(rule.rule_id for rule in pipeline.rules),
            }
        ),
        lookahead_guard=chosen.lookahead_guard,
        strategies=tuple(
            _strategy_identity(instance)
            for instance in configuration.strategy_instances
        ),
    )
