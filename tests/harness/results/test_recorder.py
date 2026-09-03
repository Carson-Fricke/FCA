"""Focused tests for the streaming run recorder and its audit artifacts."""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from harness.core.clock import Clock, SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types.accounting import AccountSnapshot, ExecutionReport, Fill
from harness.core.types.codecs import ParquetCodec
from harness.core.types.events import EventType, QuoteEvent, TradeEvent
from harness.core.types.orders import (
    ExecutionType,
    MultiLegOrder,
    OrderLeg,
    OrderStatus,
    OrderType,
    Side,
    SingleLegOrder,
    TimeInForce,
)
from harness.portfolio import PortfolioHub, PositionMarker
from harness.results.recorder import (
    ActionKind,
    Artifact,
    MarkerScope,
    RecorderConfig,
    RunManifest,
    RunRecorder,
    TransitionReason,
    artifact_path,
    read_artifact,
    read_rows,
)
from harness.risk.models import RiskAction, RiskAudit, RiskDecision
from harness.strategy.context import Emitter, StrategyContext

SPY: Final = Symbol("SPY")
QQQ: Final = Symbol("QQQ")
STAMP: Final = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
EVENT_COUNT: Final = 1_000_000
SAMPLE_COUNT: Final = 200_000


def quote(ordinal: int) -> QuoteEvent:
    """Build one deterministic quote at ``ordinal`` microseconds past the open."""
    return QuoteEvent(
        event_id=f"quote-{ordinal}",
        timestamp=STAMP + timedelta(microseconds=ordinal),
        sequence=ordinal,
        symbol=SPY,
        bid_price=Decimal("100.00"),
        bid_quantity=Decimal("1"),
        ask_price=Decimal("100.02"),
        ask_quantity=Decimal("1"),
    )


def trade(ordinal: int) -> TradeEvent:
    """Build one deterministic trade at ``ordinal`` microseconds past the open."""
    return TradeEvent(
        event_id=f"trade-{ordinal}",
        timestamp=STAMP + timedelta(microseconds=ordinal),
        sequence=ordinal,
        symbol=SPY,
        price=Decimal("100.01"),
        quantity=Decimal("2"),
    )


def config(
    directory: Path,
    *,
    run_id: str = "scripted-run",
    seed: str | int = "recorder-seed",
    batch_size: int = 8,
    record_events: bool = True,
    record_payloads: bool = True,
    clock: SimClock | None = None,
) -> RecorderConfig:
    """Build one recorder configuration for a deterministic run."""
    return RecorderConfig(
        run_id=run_id,
        directory=directory,
        seed=seed,
        batch_size=batch_size,
        record_events=record_events,
        record_payloads=record_payloads,
        clock=clock,
        strategy_id="momentum",
        book_id="alpha",
        account_id="paper",
    )


@dataclass(frozen=True, slots=True)
class ScriptedRun:
    """Identifiers and manifest produced by one scripted recorder run."""

    manifest: RunManifest
    directory: Path
    action_ids: tuple[str, ...]
    order_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]
    snapshot_ids: tuple[str, ...]


def scripted_run(
    directory: Path,
    *,
    run_id: str = "scripted-run",
    seed: str | int = "recorder-seed",
    batch_size: int = 8,
) -> ScriptedRun:
    """Drive one complete, scripted run through a fresh recorder."""
    clock = SimClock(STAMP)
    recorder = RunRecorder(
        config(directory, run_id=run_id, seed=seed, batch_size=batch_size, clock=clock)
    )
    hub = PortfolioHub("paper")
    hub.create_book("alpha", Decimal("100000"))

    entry_action = recorder.record_action(
        ActionKind.SUBMIT,
        symbol=SPY,
        side=Side.BUY,
        quantity=Decimal("10"),
        reason="momentum entry",
    )
    entry_order = SingleLegOrder(
        event_id="event-order-1",
        timestamp=clock.now(),
        sequence=1,
        order_id="order-1",
        symbol=SPY,
        side=Side.BUY,
        quantity=Decimal("10"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=Decimal("100.02"),
    )
    recorder.record_order(entry_order, action_id=entry_action)
    entry_decision = recorder.record_risk_decision(
        RiskDecision(
            RiskAction.ALLOW,
            entry_order,
            entry_order,
            audits=(
                RiskAudit(
                    sequence=1,
                    pass_number=1,
                    rule_id="max-order-notional",
                    action=RiskAction.ALLOW,
                    order_before=entry_order,
                    order_after=entry_order,
                    reason=None,
                    metrics={"notional": Decimal("1000.20")},
                    recheck=False,
                ),
            ),
            strategy_id="momentum",
        ),
        action_id=entry_action,
    )
    recorder.record_execution_report(
        ExecutionReport(
            event_id="event-report-1",
            timestamp=clock.now(),
            sequence=2,
            report_id="report-1",
            order_id="order-1",
            status=OrderStatus.FILLED,
            execution_type=ExecutionType.FILL,
            order_quantity=Decimal("10"),
            filled_quantity=Decimal("10"),
            remaining_quantity=Decimal("0"),
            average_fill_price=Decimal("100.02"),
            last_fill_quantity=Decimal("10"),
            last_fill_price=Decimal("100.02"),
        )
    )
    entry_fill = Fill(
        event_id="event-fill-1",
        timestamp=clock.now(),
        sequence=3,
        fill_id="fill-1",
        order_id="order-1",
        symbol=SPY,
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100.02"),
        fee=Decimal("0.05"),
        liquidity="removed",
    )
    entry_transition = recorder.reserve_id("position")
    entry_snapshot = recorder.reserve_id("equity")
    recorder.record_fill(
        entry_fill,
        action_id=entry_action,
        risk_decision_id=entry_decision,
        position_transition_id=entry_transition,
        equity_snapshot_id=entry_snapshot,
    )
    position = hub.apply_fill("alpha", entry_fill)
    recorded_transition = recorder.record_position_transition(
        position,
        reason=TransitionReason.FILL,
        fill_id=entry_fill.fill_id,
        transition_id=entry_transition,
    )
    hub.mark(SPY, Decimal("101.00"))
    recorder.record_position_marker(PositionMarker(SPY, Decimal("101.00")))
    recorded_snapshot = recorder.record_equity_snapshot(
        hub.snapshot, snapshot_id=entry_snapshot
    )
    recorder.record_equity_snapshot(hub.book("alpha").snapshot)
    assert recorded_transition == entry_transition
    assert recorded_snapshot == entry_snapshot

    exit_action = recorder.record_action(
        ActionKind.SUBMIT,
        symbol=SPY,
        side=Side.SELL,
        quantity=Decimal("10"),
        reason="momentum exit",
    )
    spread_order = MultiLegOrder(
        event_id="event-order-2",
        timestamp=clock.now(),
        sequence=4,
        order_id="order-2",
        legs=(
            OrderLeg(SPY, Side.SELL, Decimal("10")),
            OrderLeg(QQQ, Side.BUY, Decimal("10")),
        ),
        order_type=OrderType.MARKET,
    )
    recorder.record_order(spread_order, action_id=exit_action)
    exit_decision = recorder.record_risk_decision(
        RiskDecision(
            RiskAction.REJECT,
            spread_order,
            spread_order,
            audits=(
                RiskAudit(
                    sequence=2,
                    pass_number=1,
                    rule_id="max-order-notional",
                    action=RiskAction.REJECT,
                    order_before=spread_order,
                    order_after=spread_order,
                    reason="notional limit",
                    metrics={"notional": Decimal("90000.00")},
                    recheck=True,
                ),
            ),
            reasons=("notional limit",),
            strategy_id="momentum",
        ),
        action_id=exit_action,
    )
    cancel_action = recorder.record_action(
        ActionKind.CANCEL,
        order_id="order-2",
        reason="halt after rejection",
    )
    recorder.record_marker("phase", "rejected", scope=MarkerScope.RUN)
    recorder.record_emission("signal", {"strength": Decimal("0.25")}, bars=20)
    recorder.stream_events(quote(index) for index in range(4))
    manifest = recorder.close()
    return ScriptedRun(
        manifest=manifest,
        directory=directory,
        action_ids=(entry_action, exit_action, cancel_action),
        order_ids=("order-1", "order-2"),
        decision_ids=(entry_decision, exit_decision),
        transition_ids=(entry_transition,),
        snapshot_ids=(entry_snapshot,),
    )


def rows(directory: Path, artifact: Artifact) -> tuple[dict[str, object], ...]:
    """Read one artifact from a run directory."""
    return read_rows(artifact_path(directory, artifact), artifact)


def test_scripted_run_writes_every_artifact(tmp_path: Path) -> None:
    run = scripted_run(tmp_path / "run")

    for artifact in Artifact:
        path = artifact_path(run.directory, artifact)
        assert path.is_file(), artifact.value
        assert pq.read_metadata(path).num_rows >= 0
    counts = {
        artifact.value: len(rows(run.directory, artifact)) for artifact in Artifact
    }
    assert counts["actions"] == 3
    assert counts["orders"] == 2
    assert counts["risk_decisions"] == 2
    assert counts["risk_audits"] == 2
    assert counts["fills"] == 1
    assert counts["execution_reports"] == 1
    assert counts["position_transitions"] == 1
    assert counts["equity"] == 2
    assert counts["emissions"] == 1
    assert counts["markers"] == 2
    assert counts["events"] == 4
    assert counts["manifest"] > 0


def test_scripted_run_links_actions_orders_fills_and_beyond(tmp_path: Path) -> None:
    run = scripted_run(tmp_path / "run")
    actions = rows(run.directory, Artifact.ACTIONS)
    orders = rows(run.directory, Artifact.ORDERS)
    fills = rows(run.directory, Artifact.FILLS)
    decisions = rows(run.directory, Artifact.RISK_DECISIONS)
    audits = rows(run.directory, Artifact.RISK_AUDITS)
    transitions = rows(run.directory, Artifact.POSITION_TRANSITIONS)
    equity = rows(run.directory, Artifact.EQUITY)

    action_ids = {row["action_id"] for row in actions}
    assert action_ids == set(run.action_ids)
    assert {row["action_id"] for row in orders} <= action_ids
    assert {row["action_id"] for row in decisions} <= action_ids
    assert {row["action_id"] for row in fills} <= action_ids

    order_ids = {row["order_id"] for row in orders}
    assert order_ids == set(run.order_ids)
    orders_by_id = {str(row["order_id"]): row for row in orders}
    for fill in fills:
        assert fill["order_id"] in order_ids
        assert fill["action_id"] == orders_by_id[str(fill["order_id"])]["action_id"]

    decision_ids = {row["decision_id"] for row in decisions}
    assert decision_ids == set(run.decision_ids)
    assert {row["risk_decision_id"] for row in fills} == {run.decision_ids[0]}
    assert {row["decision_id"] for row in audits} == decision_ids

    transition_ids = {row["transition_id"] for row in transitions}
    assert transition_ids == set(run.transition_ids)
    assert {row["position_transition_id"] for row in fills} == transition_ids
    assert {row["fill_id"] for row in transitions} == {"fill-1"}

    snapshot_ids = {row["snapshot_id"] for row in equity}
    assert set(run.snapshot_ids) <= snapshot_ids
    assert {row["equity_snapshot_id"] for row in fills} == set(run.snapshot_ids)

    canceled = [row for row in actions if row["kind"] == ActionKind.CANCEL.value]
    assert [row["order_id"] for row in canceled] == ["order-2"]


def test_manifest_reports_row_counts_and_digests(tmp_path: Path) -> None:
    run = scripted_run(tmp_path / "run")
    manifest_rows = {
        str(row["key"]): str(row["value"])
        for row in rows(run.directory, Artifact.MANIFEST)
    }

    assert manifest_rows["run_id"] == "scripted-run"
    assert manifest_rows["seed"] == "recorder-seed"
    assert manifest_rows["batch_size"] == "8"
    for artifact in Artifact:
        if artifact is Artifact.MANIFEST:
            continue
        path = artifact_path(run.directory, artifact)
        assert manifest_rows[f"artifact:{artifact.value}"] == str(
            pq.read_metadata(path).num_rows
        )
        assert len(manifest_rows[f"digest:{artifact.value}"]) == 64
    assert len(manifest_rows["run_digest"]) == 64


def test_repeated_runs_produce_byte_identical_artifacts(tmp_path: Path) -> None:
    first = scripted_run(tmp_path / "first")
    second = scripted_run(tmp_path / "second")

    for artifact in Artifact:
        left = artifact_path(first.directory, artifact).read_bytes()
        right = artifact_path(second.directory, artifact).read_bytes()
        assert left == right, artifact.value


def test_empty_run_still_writes_every_artifact(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "empty"))
    manifest = recorder.close()

    assert manifest.total_rows == manifest.row_counts[Artifact.MANIFEST.value]
    for artifact in Artifact:
        path = artifact_path(tmp_path / "empty", artifact)
        assert path.is_file(), artifact.value
        expected = 0 if artifact is not Artifact.MANIFEST else None
        if expected is not None:
            assert pq.read_metadata(path).num_rows == expected
    assert recorder.closed
    assert recorder.close() is manifest


def test_one_million_events_stream_without_accumulating(tmp_path: Path) -> None:
    """Stream one million events, keeping at most one row batch in memory.

    The recorder flushes a batch as soon as it fills, so the pending row count
    is bounded by ``batch_size`` no matter how long the stream runs.  Peak
    process memory therefore depends on ``batch_size`` and not on stream
    length, which :func:`test_streaming_peak_memory_is_bounded` measures.
    """
    batch_size = 10_000
    recorder = RunRecorder(
        config(
            tmp_path / "million",
            run_id="million",
            seed="million",
            batch_size=batch_size,
            record_payloads=False,
        )
    )

    def stream() -> Iterator[QuoteEvent]:
        for ordinal in range(EVENT_COUNT):
            yield quote(ordinal)

    peak_buffered = 0
    recorded = 0
    for event in stream():
        recorder.record_event(event)
        recorded += 1
        peak_buffered = max(peak_buffered, recorder.buffered_rows)
    manifest = recorder.close()

    assert recorded == EVENT_COUNT
    assert peak_buffered <= batch_size
    assert recorder.buffered_rows == 0
    assert manifest.row_counts[Artifact.EVENTS.value] == EVENT_COUNT
    events_path = artifact_path(tmp_path / "million", Artifact.EVENTS)
    assert pq.read_metadata(events_path).num_rows == EVENT_COUNT


def test_streaming_peak_memory_is_bounded(tmp_path: Path) -> None:
    """Measure traced peak memory for a long stream with full payloads."""
    recorder = RunRecorder(
        config(tmp_path / "sample", run_id="sample", seed="sample", batch_size=10_000)
    )
    tracemalloc.start()
    try:
        recorded = recorder.stream_events(quote(index) for index in range(SAMPLE_COUNT))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    recorder.close()

    assert recorded == SAMPLE_COUNT
    assert peak < 64 * 1024 * 1024


def test_events_record_payloads_by_default(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "payloads"))
    recorder.record_event(quote(0))
    recorder.record_event(trade(1))
    recorder.close()

    events = rows(tmp_path / "payloads", Artifact.EVENTS)
    assert [row["event_type"] for row in events] == ["quote", "trade"]
    assert [row["symbol"] for row in events] == ["SPY", "SPY"]
    payload = json.loads(str(events[0]["payload"]))
    assert payload["bid_price"] == "100.00"
    assert payload["event_id"] == "quote-0"
    assert [row["type_name"] for row in events] == [
        "harness.core.types.events.QuoteEvent",
        "harness.core.types.events.TradeEvent",
    ]


def test_closed_recorder_rejects_further_records(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "closed", clock=SimClock(STAMP)))
    recorder.close()

    with pytest.raises(RuntimeError, match="closed"):
        recorder.record_event(quote(0))


def test_missing_timestamp_without_a_clock_is_rejected(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "no-clock"))

    with pytest.raises(ValueError, match="timestamp is required"):
        recorder.record_action(ActionKind.HOLD)


def test_injected_clock_supplies_deterministic_timestamps(tmp_path: Path) -> None:
    clock = SimClock(STAMP)
    recorder = RunRecorder(config(tmp_path / "clocked", clock=clock))
    recorder.record_action(ActionKind.HOLD, reason="wait")
    clock.advance_to(STAMP + timedelta(minutes=1))
    recorder.record_marker("phase", "entered")
    recorder.close()

    actions = rows(tmp_path / "clocked", Artifact.ACTIONS)
    markers = rows(tmp_path / "clocked", Artifact.MARKERS)
    assert actions[0]["timestamp"] == STAMP
    assert markers[0]["timestamp"] == STAMP + timedelta(minutes=1)


def test_recorder_implements_the_strategy_emitter_protocol(tmp_path: Path) -> None:
    clock = SimClock(STAMP)
    recorder = RunRecorder(config(tmp_path / "emitter", clock=clock))
    assert isinstance(recorder, Emitter)

    hub = PortfolioHub("paper")
    hub.create_book("alpha", Decimal("10000"))
    context = StrategyContext(
        clock, portfolio=hub, bus=EventBus(clock), emissions=recorder
    )
    context.emit("signal", {"strength": Decimal("0.5")})
    recorder.close()

    emissions = rows(tmp_path / "emitter", Artifact.EMISSIONS)
    assert [row["name"] for row in emissions] == ["signal"]
    assert json.loads(str(emissions[0]["fields"])) == {"strength": "0.5"}
    assert emissions[0]["strategy_id"] == "momentum"


def test_attach_to_bus_records_dispatched_events(tmp_path: Path) -> None:
    clock = SimClock(STAMP)
    bus = EventBus(clock)
    recorder = RunRecorder(config(tmp_path / "bus", clock=clock))
    subscribed = recorder.attach_to_bus(bus)
    bus.publish(quote(0))
    bus.publish(trade(1))
    dispatched = bus.drain()
    recorder.close()

    assert len(dispatched) == 2
    assert subscribed == tuple(EventType)
    events = rows(tmp_path / "bus", Artifact.EVENTS)
    assert [row["event_id"] for row in events] == ["quote-0", "trade-1"]


def test_attach_to_bus_requires_event_recording(tmp_path: Path) -> None:
    bus = EventBus(SimClock(STAMP))
    recorder = RunRecorder(config(tmp_path / "no-events", record_events=False))

    with pytest.raises(ValueError, match="record_events"):
        recorder.attach_to_bus(bus)
    with pytest.raises(TypeError, match="EventBus"):
        recorder.attach_to_bus(cast(EventBus, object()))


def test_multi_leg_orders_are_normalized_with_legs(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "multi", clock=SimClock(STAMP)))
    order = MultiLegOrder(
        event_id="event-spread",
        timestamp=STAMP,
        sequence=0,
        order_id="order-spread",
        legs=(
            OrderLeg(SPY, Side.SELL, Decimal("1")),
            OrderLeg(QQQ, Side.BUY, Decimal("2")),
        ),
        order_type=OrderType.MARKET,
    )
    recorder.record_order(order, action_id="action-1")
    recorder.close()

    recorded = rows(tmp_path / "multi", Artifact.ORDERS)
    assert len(recorded) == 1
    row = recorded[0]
    assert row["kind"] == "multi"
    assert row["symbol"] is None
    assert row["side"] is None
    assert row["quantity"] is None
    assert row["action_id"] == "action-1"
    legs = json.loads(str(row["legs"]))
    assert [(leg["symbol"], leg["side"], leg["quantity"]) for leg in legs] == [
        ("SPY", "sell", "1"),
        ("QQQ", "buy", "2"),
    ]


def test_execution_reports_round_trip_through_the_core_codec(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "reports", clock=SimClock(STAMP)))
    recorder.record_execution_report(
        ExecutionReport(
            event_id="event-report",
            timestamp=STAMP,
            sequence=0,
            report_id="report-1",
            order_id="order-1",
            status=OrderStatus.PARTIALLY_FILLED,
            execution_type=ExecutionType.PARTIAL_FILL,
            order_quantity=Decimal("10"),
            filled_quantity=Decimal("4"),
            remaining_quantity=Decimal("6"),
            average_fill_price=Decimal("100.01"),
            last_fill_quantity=Decimal("4"),
            last_fill_price=Decimal("100.01"),
        )
    )
    recorder.close()

    path = artifact_path(tmp_path / "reports", Artifact.EXECUTION_REPORTS)
    reports = ParquetCodec.read(path, ExecutionReport)
    assert len(reports) == 1
    assert reports[0].report_id == "report-1"
    assert reports[0].filled_quantity == Decimal("4")


def test_read_artifact_rejects_schema_drift(tmp_path: Path) -> None:
    drifted = tmp_path / "drifted.parquet"
    pq.write_table(
        pa.Table.from_pylist([{"key": "a", "value": "b"}]),
        drifted,
        compression="zstd",
        version="2.6",
    )

    with pytest.raises(ValueError, match="schema does not match"):
        read_artifact(drifted, Artifact.EVENTS)


def test_equity_snapshots_accept_book_portfolio_and_account(tmp_path: Path) -> None:
    clock = SimClock(STAMP)
    recorder = RunRecorder(config(tmp_path / "equity", clock=clock))
    hub = PortfolioHub("paper")
    hub.create_book("alpha", Decimal("1000"))
    hub.mark(SPY, Decimal("10.00"))
    recorder.record_equity_snapshot(hub.snapshot)
    recorder.record_equity_snapshot(hub.book("alpha").snapshot)
    recorder.record_equity_snapshot(
        AccountSnapshot(
            event_id="event-account",
            timestamp=STAMP,
            sequence=0,
            account_id="paper",
            cash=Decimal("500.00"),
            equity=Decimal("600.00"),
            buying_power=Decimal("1000.00"),
        )
    )
    recorder.close()

    equity = rows(tmp_path / "equity", Artifact.EQUITY)
    assert [row["source"] for row in equity] == ["portfolio", "book", "account"]
    assert [row["cash"] for row in equity] == ["1000", "1000", "500.00"]
    assert [row["book_id"] for row in equity] == [None, "alpha", None]
    assert [row["account_id"] for row in equity] == ["paper", "paper", "paper"]


def test_reserved_identifiers_are_deterministic_and_unique(tmp_path: Path) -> None:
    first = RunRecorder(config(tmp_path / "first", clock=SimClock(STAMP)))
    second = RunRecorder(config(tmp_path / "second", clock=SimClock(STAMP)))

    first_ids = tuple(first.reserve_id("position") for _ in range(3))
    second_ids = tuple(second.reserve_id("position") for _ in range(3))
    first.close()
    second.close()

    assert first_ids == second_ids
    assert len(set(first_ids)) == 3


def test_recorder_rejects_unsupported_records_and_values(tmp_path: Path) -> None:
    recorder = RunRecorder(config(tmp_path / "invalid", clock=SimClock(STAMP)))

    with pytest.raises(TypeError, match="Fill"):
        recorder.record_fill(cast(Fill, quote(0)))
    with pytest.raises(TypeError, match="binary floating point"):
        recorder.record_emission("signal", {"value": 1.5})
    with pytest.raises(TypeError, match="EventBus"):
        recorder.attach_to_bus(cast(EventBus, object()))
    recorder.close()


@pytest.mark.parametrize(
    ("build", "expected", "pattern"),
    [
        (
            lambda path: RecorderConfig(run_id="  ", directory=path),
            ValueError,
            "run_id",
        ),
        (
            lambda path: RecorderConfig(run_id="run", directory=path, batch_size=0),
            ValueError,
            "batch_size",
        ),
        (
            lambda path: RecorderConfig(run_id="run", directory=path, batch_size=True),
            TypeError,
            "batch_size",
        ),
        (
            lambda path: RecorderConfig(run_id="run", directory=path, compression=" "),
            ValueError,
            "compression",
        ),
        (
            lambda path: RecorderConfig(
                run_id="run", directory=path, parquet_version=""
            ),
            ValueError,
            "parquet_version",
        ),
        (
            lambda path: RecorderConfig(
                run_id="run", directory=path, clock=cast(Clock, object())
            ),
            TypeError,
            "Clock",
        ),
        (
            lambda path: RecorderConfig(run_id="run", directory=path, strategy_id=""),
            ValueError,
            "strategy_id",
        ),
    ],
)
def test_invalid_configurations_are_rejected(
    tmp_path: Path,
    build: Callable[[Path], RecorderConfig],
    expected: type[Exception],
    pattern: str,
) -> None:
    with pytest.raises(expected, match=pattern):
        build(tmp_path / "rejected")
