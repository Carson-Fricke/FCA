"""Performance and memory benchmarks for the harness engine paths.

Benchmarks are skipped unless the ``FCA_PERF`` environment variable is
``1``, so normal test and CI runs are unaffected.  Measurement uses the
standard library only (``time.perf_counter_ns``, ``statistics``, and
``tracemalloc``), every workload is deterministic and synthetic, and
results are recorded as baselines rather than asserted as absolute times.
Wall-clock measurement is allowed here because benchmarks are not part of
the synchronous kernel and never run in the default CI path.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import time
import tracemalloc
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest
from _pytest.terminal import TerminalReporter

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import OrderType, Side, SingleLegOrder, TimeInForce
from harness.data.replay import LakeReplayFeed
from harness.fills import SimpleTouch, TradeThrough
from harness.portfolio.hub import PortfolioSnapshot
from harness.results.recorder import RecorderConfig, RunRecorder
from harness.risk.limits import (
    MaxGrossExposure,
    MaxOrderCount,
    MaxOrderNotional,
    MaxOrderSize,
)
from harness.risk.models import RiskAction, RiskContext, RiskRule
from harness.risk.pipeline import RiskPipeline
from lake.contracts import ExchangeSession

PERFORMANCE_GATE: Final = "FCA_PERF"
PERFORMANCE_MARKER: Final = "performance"
REGRESSION_MODULE: Final = "test_regression_report.py"

STAMP: Final = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
SESSION_DAY: Final = date(2024, 1, 2)
TIER_WIDTHS: Final[Mapping[str, int]] = MappingProxyType(
    {"narrow": 1, "medium": 8, "wide": 32}
)

REPLAY_EVENTS: Final = 20_000
MATCHING_ORDERS: Final = 20_000
RISK_DECISIONS: Final = 10_000
RECORDER_ITERATIONS: Final = 5_000
RECORDER_ROWS_PER_ITERATION: Final = 4
RECORDER_ROWS: Final = RECORDER_ITERATIONS * RECORDER_ROWS_PER_ITERATION
MEMORY_EVENTS: Final = 10_000
TIMING_REPEATS: Final = 5
TIMING_WARMUP: Final = 1
MEMORY_REPEATS: Final = 3
TIMING_RUNS: Final = TIMING_WARMUP + TIMING_REPEATS

EXPECTED_METRICS: Final = (
    "replay.throughput.narrow",
    "replay.throughput.medium",
    "replay.throughput.wide",
    "matching.simple_touch.cost_per_order",
    "matching.trade_through.cost_per_order",
    "risk.pipeline.allow.latency_per_decision",
    "risk.pipeline.reduce.latency_per_decision",
    "recorder.artifacts.rows_per_second",
    "memory.streaming.peak_mib.narrow",
    "memory.streaming.peak_mib.medium",
    "memory.streaming.peak_mib.wide",
    "memory.streaming.peak_bytes_per_event.narrow",
    "memory.streaming.peak_bytes_per_event.medium",
    "memory.streaming.peak_bytes_per_event.wide",
)


@dataclass(frozen=True, slots=True)
class Metric:
    """One measured benchmark value with its unit and regression direction."""

    name: str
    value: float
    unit: str
    higher_is_better: bool


class BenchmarkSuite:
    """Deterministic synthetic workloads with stdlib-only measurement.

    The suite records measured values instead of asserting absolute times,
    which keeps results meaningful across machine speeds.  The regression
    report compares captured baselines against a fresh run using a 20
    percent budget in the unfavorable direction.
    """

    def __init__(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        self._tmp_path_factory = tmp_path_factory
        self.results: dict[str, Metric] = {}

    # -- benchmark entry points ------------------------------------------

    def benchmark_replay_throughput(self, tier: str) -> Metric:
        """Measure merged replay throughput in events per second."""
        feed, events = self._replay_feed(tier, REPLAY_EVENTS)
        assert events == REPLAY_EVENTS
        elapsed_ns = self._median_ns([self._drain(feed)] * TIMING_RUNS)
        throughput = events / (elapsed_ns / 1_000_000_000)
        return self._record(
            f"replay.throughput.{tier}", round(throughput, 2), "events/s", True
        )

    def benchmark_matching_cost(self, model: str) -> Metric:
        """Measure fill-model decision cost per order in microseconds."""
        orders, markets = self._matching_workload(model)
        fill_model: SimpleTouch | TradeThrough = (
            SimpleTouch() if model == "simple_touch" else TradeThrough()
        )
        decide_all = self._decide_all(fill_model, orders, markets)
        assert decide_all() == len(orders)
        elapsed_ns = self._median_ns([decide_all] * TIMING_RUNS)
        per_order_us = elapsed_ns / len(orders) / 1_000
        return self._record(
            f"matching.{model}.cost_per_order",
            round(per_order_us, 4),
            "us/order",
            False,
        )

    def benchmark_risk_latency(self, path: str) -> Metric:
        """Measure risk-pipeline latency per decision in microseconds."""
        pipeline, order, context = self._risk_workload(path)
        expected = RiskAction.ALLOW if path == "allow" else RiskAction.REDUCE
        assert pipeline.evaluate(order, context).action is expected
        evaluate_all = self._evaluate_all(pipeline, order, context)
        elapsed_ns = self._median_ns([evaluate_all] * TIMING_RUNS)
        per_decision_us = elapsed_ns / RISK_DECISIONS / 1_000
        return self._record(
            f"risk.pipeline.{path}.latency_per_decision",
            round(per_decision_us, 4),
            "us/decision",
            False,
        )

    def benchmark_recorder_throughput(self) -> Metric:
        """Measure streaming artifact write throughput in rows per second."""
        order = self._order(
            "perf-recorder-order",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100.02"),
        )
        fill = Fill(
            event_id="perf-recorder-fill-event",
            timestamp=STAMP,
            sequence=1,
            fill_id="perf-recorder-fill",
            order_id="perf-recorder-order",
            symbol=Symbol("SPY"),
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.02"),
            fee=Decimal("0.01"),
            liquidity="taker",
        )
        base = self._tmp_path_factory.mktemp("perf-recorder")
        runs = count()

        def run() -> None:
            recorder = RunRecorder(
                self._recorder_config(base / f"run-{next(runs):04d}")
            )
            for _iteration in range(RECORDER_ITERATIONS):
                recorder.record_action(
                    "submit",
                    symbol=Symbol("SPY"),
                    side=Side.BUY,
                    quantity=Decimal("10"),
                    reason="performance workload",
                    at=STAMP,
                )
                recorder.record_order(order, at=STAMP)
                recorder.record_fill(fill)
                recorder.record_marker("perf", "tick", at=STAMP)
            recorder.close()

        elapsed_ns = self._median_ns([run] * TIMING_RUNS)
        rows_per_second = RECORDER_ROWS / (elapsed_ns / 1_000_000_000)
        return self._record(
            "recorder.artifacts.rows_per_second",
            round(rows_per_second, 2),
            "rows/s",
            True,
        )

    def benchmark_memory_profile(self, tier: str) -> tuple[Metric, Metric]:
        """Measure streaming peak memory across one representative profile."""
        feed, events = self._replay_feed(tier, MEMORY_EVENTS)
        base = self._tmp_path_factory.mktemp("perf-memory")
        peaks = [
            self._streaming_peak(feed, base / f"run-{run:04d}")
            for run in range(MEMORY_REPEATS)
        ]
        peak, processed = min(peaks, key=lambda entry: entry[0])
        assert processed == events
        return (
            self._record(
                f"memory.streaming.peak_mib.{tier}",
                round(peak / (1024 * 1024), 3),
                "MiB",
                False,
            ),
            self._record(
                f"memory.streaming.peak_bytes_per_event.{tier}",
                round(peak / events, 1),
                "bytes/event",
                False,
            ),
        )

    def run_all(self) -> Mapping[str, Metric]:
        """Run every benchmark once, reusing complete in-session results."""
        if all(name in self.results for name in EXPECTED_METRICS):
            return self.results
        for tier in TIER_WIDTHS:
            self.benchmark_replay_throughput(tier)
        for model in ("simple_touch", "trade_through"):
            self.benchmark_matching_cost(model)
        for path in ("allow", "reduce"):
            self.benchmark_risk_latency(path)
        self.benchmark_recorder_throughput()
        for tier in TIER_WIDTHS:
            self.benchmark_memory_profile(tier)
        missing = [name for name in EXPECTED_METRICS if name not in self.results]
        if missing:
            raise RuntimeError(f"benchmarks did not produce metrics: {missing}")
        return self.results

    def write_baselines(self, path: Path) -> None:
        """Write the complete fresh run as the captured baseline document."""
        metrics = self.run_all()
        document = {
            "schema": "fca-performance-baselines-v1",
            "regression_budget_percent": 20.0,
            "captured_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "processor": platform.processor() or platform.machine(),
            },
            "workload": {
                "universe_tiers": dict(TIER_WIDTHS),
                "replay_events_per_tier": REPLAY_EVENTS,
                "matching_orders": MATCHING_ORDERS,
                "risk_decisions": RISK_DECISIONS,
                "recorder_iterations": RECORDER_ITERATIONS,
                "recorder_rows": RECORDER_ROWS,
                "memory_events_per_tier": MEMORY_EVENTS,
                "timing_warmup": TIMING_WARMUP,
                "timing_repeats": TIMING_REPEATS,
                "memory_repeats": MEMORY_REPEATS,
            },
            "metrics": {
                name: {
                    "value": metrics[name].value,
                    "unit": metrics[name].unit,
                    "higher_is_better": metrics[name].higher_is_better,
                }
                for name in EXPECTED_METRICS
            },
        }
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    # -- measurement primitives -------------------------------------------

    def _record(
        self, name: str, value: float, unit: str, higher_is_better: bool
    ) -> Metric:
        metric = Metric(
            name=name, value=value, unit=unit, higher_is_better=higher_is_better
        )
        self.results[name] = metric
        return metric

    @staticmethod
    def _median_ns(actions: Sequence[Callable[[], object]]) -> float:
        """Return the median elapsed nanoseconds over the measured runs."""
        if len(actions) < TIMING_WARMUP + 1:
            raise ValueError("benchmarks need one warmup and one measured run")
        for action in actions[:TIMING_WARMUP]:
            action()
        samples: list[int] = []
        for action in actions[TIMING_WARMUP:]:
            started = time.perf_counter_ns()
            action()
            samples.append(time.perf_counter_ns() - started)
        return float(statistics.median(samples))

    # -- workloads ----------------------------------------------------------

    @staticmethod
    def _drain(feed: LakeReplayFeed) -> Callable[[], int]:
        def drain() -> int:
            return sum(1 for _ in feed)

        return drain

    @staticmethod
    def _decide_all(
        fill_model: SimpleTouch | TradeThrough,
        orders: Sequence[SingleLegOrder],
        markets: Sequence[QuoteEvent | TradeEvent],
    ) -> Callable[[], int]:
        def decide_all() -> int:
            decided = 0
            for order, market in zip(orders, markets, strict=True):
                if fill_model.decide(order, market) is not None:
                    decided += 1
            return decided

        return decide_all

    @staticmethod
    def _evaluate_all(
        pipeline: RiskPipeline,
        order: SingleLegOrder,
        context: RiskContext,
    ) -> Callable[[], int]:
        def evaluate_all() -> int:
            for _ in range(RISK_DECISIONS):
                pipeline.evaluate(order, context)
            return RISK_DECISIONS

        return evaluate_all

    @staticmethod
    def _streaming_peak(feed: LakeReplayFeed, directory: Path) -> tuple[int, int]:
        """Stream the feed through the bus into a recorder under tracemalloc."""
        config = RecorderConfig(
            run_id="perf-memory",
            directory=directory,
            seed="perf-memory",
            record_events=True,
            record_payloads=True,
            strategy_id="momentum",
            book_id="alpha",
            account_id="paper",
        )
        tracemalloc.start()
        try:
            bus = EventBus(SimClock(STAMP))
            recorder = RunRecorder(config)
            recorder.attach_to_bus(bus)
            processed = 0
            for event in feed:
                bus.publish(event)
                processed += len(bus.drain())
            recorder.close()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak, processed

    def _replay_feed(self, tier: str, total_events: int) -> tuple[LakeReplayFeed, int]:
        """Build one deterministic merged replay feed for a universe tier."""
        width = TIER_WIDTHS[tier]
        per_symbol = total_events // width
        streams: dict[object, object] = {}
        for index in range(width):
            symbol = f"S{index:03d}"
            events: list[QuoteEvent | TradeEvent] = []
            for ordinal in range(per_symbol):
                if ordinal % 2 == 0:
                    events.append(self._quote(symbol, ordinal))
                else:
                    events.append(self._trade(symbol, ordinal))
            streams[symbol] = tuple(events)
        return LakeReplayFeed((self._session(),), streams=streams), per_symbol * width

    def _matching_workload(
        self, model: str
    ) -> tuple[list[SingleLegOrder], list[QuoteEvent | TradeEvent]]:
        """Build one deterministic order/market pairing per fill model."""
        orders: list[SingleLegOrder] = []
        markets: list[QuoteEvent | TradeEvent] = []
        for ordinal in range(MATCHING_ORDERS):
            side = Side.BUY if ordinal % 2 == 0 else Side.SELL
            if model == "simple_touch":
                orders.append(
                    self._order(
                        f"perf-order-{ordinal:06d}",
                        side=side,
                        order_type=OrderType.MARKET,
                    )
                )
                markets.append(self._quote("SPY", ordinal % 8))
            elif model == "trade_through":
                orders.append(
                    self._order(
                        f"perf-order-{ordinal:06d}",
                        side=side,
                        order_type=OrderType.LIMIT,
                        limit_price=Decimal("100.50"),
                    )
                )
                markets.append(self._trade("SPY", ordinal % 8))
            else:
                raise ValueError(f"unknown fill model: {model!r}")
        return orders, markets

    @staticmethod
    def _risk_workload(
        path: str,
    ) -> tuple[RiskPipeline, SingleLegOrder, RiskContext]:
        """Build one deterministic risk pipeline workload."""
        order = SingleLegOrder(
            event_id="perf-risk-order-event",
            timestamp=STAMP,
            sequence=1,
            order_id="perf-risk-order",
            symbol=Symbol("SPY"),
            side=Side.BUY,
            quantity=Decimal("10"),
        )
        portfolio = PortfolioSnapshot(
            account_id="perf",
            initial_cash=Decimal("100000"),
            cash=Decimal("100000"),
            positions=(),
            books=(),
            fill_count=0,
        )
        context = RiskContext(
            portfolio=portfolio,
            prices={Symbol("SPY"): Decimal("100.00")},
            daily_loss=Decimal("0"),
            order_count=0,
            strategy_id="momentum",
        )
        if path == "allow":
            rules: tuple[RiskRule, ...] = (
                MaxOrderNotional({"limit": Decimal("1000000")}),
                MaxOrderSize({"limit": Decimal("1000000")}),
                MaxGrossExposure({"limit": Decimal("10000000")}),
                MaxOrderCount({"limit": Decimal("1000000")}),
            )
        elif path == "reduce":
            rules = (MaxOrderNotional({"limit": Decimal("800")}),)
        else:
            raise ValueError(f"unknown risk benchmark path: {path!r}")
        return RiskPipeline(rules), order, context

    @staticmethod
    def _recorder_config(directory: Path) -> RecorderConfig:
        return RecorderConfig(
            run_id="perf-recorder",
            directory=directory,
            seed="perf-recorder",
            batch_size=4096,
            record_events=False,
            record_payloads=True,
            strategy_id="momentum",
            book_id="alpha",
            account_id="paper",
        )

    @staticmethod
    def _session() -> ExchangeSession:
        start = datetime.combine(SESSION_DAY, datetime.min.time(), UTC)
        return ExchangeSession(SESSION_DAY, start, start + timedelta(days=1))

    @staticmethod
    def _quote(symbol: str, ordinal: int) -> QuoteEvent:
        bid = Decimal("100.00") + Decimal("0.01") * (ordinal % 5)
        return QuoteEvent(
            f"perf-quote-{symbol}-{ordinal:06d}",
            STAMP + timedelta(microseconds=ordinal),
            ordinal,
            Symbol(symbol),
            bid,
            Decimal("1000"),
            bid + Decimal("0.02"),
            Decimal("1000"),
        )

    @staticmethod
    def _trade(symbol: str, ordinal: int) -> TradeEvent:
        price = Decimal("100.45") if ordinal % 2 == 0 else Decimal("100.55")
        return TradeEvent(
            f"perf-trade-{symbol}-{ordinal:06d}",
            STAMP + timedelta(microseconds=ordinal),
            ordinal,
            Symbol(symbol),
            price,
            Decimal("1000"),
        )

    @staticmethod
    def _order(
        order_id: str,
        *,
        side: Side,
        order_type: OrderType,
        limit_price: Decimal | None = None,
    ) -> SingleLegOrder:
        return SingleLegOrder(
            event_id=f"perf-event-{order_id}",
            timestamp=STAMP,
            sequence=1,
            order_id=order_id,
            symbol=Symbol("SPY"),
            side=side,
            quantity=Decimal("10"),
            order_type=order_type,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )


_SUITE: BenchmarkSuite | None = None


@pytest.fixture(scope="session")
def perf(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BenchmarkSuite]:
    """Expose the shared benchmark suite for one pytest session."""
    global _SUITE
    _SUITE = BenchmarkSuite(tmp_path_factory)
    yield _SUITE


def pytest_configure(config: pytest.Config) -> None:
    """Register the performance marker for strict-marker runs."""
    config.addinivalue_line(
        "markers",
        "performance: wall-clock benchmark suite gated behind FCA_PERF=1",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip benchmarks by default and run the regression report last."""
    if os.environ.get(PERFORMANCE_GATE) != "1":
        skip = pytest.mark.skip(
            reason="performance benchmarks run only when FCA_PERF=1"
        )
        for item in items:
            if PERFORMANCE_MARKER in item.keywords:
                item.add_marker(skip)
        return
    regression = [item for item in items if item.path.name == REGRESSION_MODULE]
    if regression and len(regression) < len(items):
        rest = [item for item in items if item.path.name != REGRESSION_MODULE]
        items[:] = [*rest, *regression]


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Print measured benchmark values after a gated run."""
    suite = _SUITE
    if suite is None or not suite.results:
        return
    terminalreporter.section("performance benchmark results")
    for metric in suite.results.values():
        terminalreporter.write_line(
            f"{metric.name:<48}{metric.value:>18,.2f}  {metric.unit}"
        )
