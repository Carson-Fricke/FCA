"""Recovery-policy behavior and chaos tests.

The tests prove the recovery taxonomy end to end: resume-reconcile restores a
snapshot, reconciles through the shared runtime reconciler, and continues only
on a proceeding decision; injected broker divergence follows both the adopt
and halt policies with complete diff artifacts; flatten-halt cancels open
orders in deterministic order, flattens every position to cash through hub
accounting, and halts; halt-only preserves state untouched and halts; and
every injected fault — unreadable or torn snapshots, a broker port unavailable
mid-reconcile, failing cancel commands, missing marks, even a failing halt
mechanism — deterministically halts with an audited report instead of
proceeding silently.  Twenty randomized kill and restore cycles prove that a
resumed run reaches exactly the uninterrupted simulated end state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from random import Random
from typing import cast

import pytest

from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.symbols import Symbol
from harness.core.types import Fill, Side
from harness.portfolio import PortfolioHub, PositionMarker
from harness.risk.kill_switch import KillSwitch, KillSwitchAction
from harness.runtime.reconciler import (
    BrokerAccountRecord,
    BrokerPositionRecord,
    BrokerStateSnapshot,
    ReconcileDecision,
    ReconcileTrigger,
    RuntimeReconciler,
    StaticPolicy,
)
from harness.runtime.recovery import (
    BootPolicyKind,
    FlattenHaltPolicy,
    HaltOnlyPolicy,
    KillSwitchHaltPort,
    RecoveryAction,
    RecoveryError,
    RecoveryPhase,
    RecoveryReport,
    ResumeReconcilePolicy,
    RuntimeRecovery,
    cancel_all,
    flatten_positions,
)
from harness.runtime.state_store import (
    FORMAT_VERSION,
    BookState,
    RiskState,
    RuntimeSnapshot,
    RuntimeStateStore,
    SnapshotTrigger,
)

RUN_ID = "paper-recovery-1"
CASH = Decimal("100000")
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
EVENT_TIME = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
ZERO = Decimal("0")
_REPORT_DOMAIN = b"fca:harness-runtime-recovery-report:v1\x00"


# --------------------------------------------------------------------------
# Deterministic fakes
# --------------------------------------------------------------------------


class MemorySnapshotStore:
    """In-memory adapter for the snapshot object-store port."""

    __slots__ = ("_objects",)

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._objects[relative_path] = bytes(payload)

    def read(self, relative_path: str) -> bytes:
        try:
            return self._objects[relative_path]
        except KeyError:
            raise KeyError(relative_path) from None


class FakeBrokerStatePort:
    """Deterministic broker port serving one injectable state snapshot."""

    def __init__(self, state: BrokerStateSnapshot | Exception) -> None:
        self.state = state

    def fetch_state(self) -> BrokerStateSnapshot:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class RecordingCancelPort:
    """Cancel port recording its command order with fault injection."""

    def __init__(
        self, *, fail_on: frozenset[str] = frozenset(), strict: bool = False
    ) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.strict = strict

    def cancel_order(self, order_id: str) -> None:
        if order_id in self.calls and self.strict:
            raise AssertionError(f"order {order_id!r} was already canceled")
        if order_id in self.fail_on:
            raise OSError(f"cancel for {order_id!r} failed")
        self.calls.append(order_id)


class RecordingHaltPort:
    """Halt port recording every dispatched reason with fault injection."""

    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.fail = False

    def halt(self, *, reason: str) -> None:
        if self.fail:
            raise OSError("halt mechanism unavailable")
        self.reasons.append(reason)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _fill(
    index: int,
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: Decimal = Decimal("10"),
    price: Decimal = Decimal("50"),
) -> Fill:
    return Fill(
        event_id=f"evt-{index}",
        timestamp=OPEN + timedelta(minutes=index),
        sequence=index,
        fill_id=f"fill-{index}",
        order_id=f"order-{index}",
        symbol=Symbol(symbol),
        side=side,
        quantity=quantity,
        price=price,
    )


def _hub(book_fills: dict[str, list[Fill]]) -> PortfolioHub:
    hub = PortfolioHub(RUN_ID)
    for book_id in sorted(book_fills):
        hub.create_book(book_id, CASH)
    for book_id in sorted(book_fills):
        for fill in book_fills[book_id]:
            hub.apply_fill(book_id, fill)
    return hub


def _snapshot(
    hub: PortfolioHub,
    fills: list[Fill],
    *,
    prices: dict[str, Decimal] | None = None,
    run_id: str = RUN_ID,
) -> RuntimeSnapshot:
    by_id = {fill.fill_id: fill for fill in fills}
    books = tuple(
        BookState(
            book_id=book.book_id,
            initial_cash=book.initial_cash,
            cash=book.cash,
            fill_ids=book.fill_ids,
            fills=tuple(by_id[fill_id] for fill_id in book.fill_ids),
            markers=book.snapshot.markers,
        )
        for book in hub.books
    )
    return RuntimeSnapshot(
        format_version=FORMAT_VERSION,
        run_id=run_id,
        trigger=SnapshotTrigger.STOP,
        sequence=0,
        dispatched_count=0,
        market_event_count=0,
        options={"run_id": run_id, "initial_cash": CASH},
        books=books,
        prices=prices or {},
        order_counts={},
        order_owners={},
        strategy_state=(),
        random_states=(),
        risk=RiskState(on_reject="reject_order", rule_ids=(), daily_losses={}),
    )


def _store(content: MemorySnapshotStore | None = None) -> RuntimeStateStore:
    return RuntimeStateStore(
        content if content is not None else MemorySnapshotStore(), run_id=RUN_ID
    )


def _clean_state(hub: PortfolioHub) -> BrokerStateSnapshot:
    positions = tuple(
        BrokerPositionRecord(
            str(position.symbol), position.quantity, position.average_cost
        )
        for position in hub.snapshot.positions
        if position.quantity != ZERO
    )
    return BrokerStateSnapshot(
        account=BrokerAccountRecord("acct-1", hub.snapshot.cash),
        positions=positions,
    )


def _reconciler(
    state: BrokerStateSnapshot | Exception,
    *,
    policy: StaticPolicy | None = None,
) -> RuntimeReconciler:
    return RuntimeReconciler(
        broker=FakeBrokerStatePort(state), run_id=RUN_ID, policy=policy
    )


def _recovery(
    policy: ResumeReconcilePolicy | FlattenHaltPolicy | HaltOnlyPolicy,
    halt: RecordingHaltPort,
    *,
    cancel: RecordingCancelPort | None = None,
    reconciler: RuntimeReconciler | None = None,
) -> RuntimeRecovery:
    return RuntimeRecovery(
        run_id=RUN_ID, policy=policy, halt=halt, cancel=cancel, reconciler=reconciler
    )


def _phases(report: RecoveryReport) -> tuple[RecoveryPhase, ...]:
    return tuple(step.phase for step in report.steps)


# --------------------------------------------------------------------------
# Construction and validation
# --------------------------------------------------------------------------


def test_resume_reconcile_requires_a_reconciler() -> None:
    with pytest.raises(RecoveryError, match="RuntimeReconciler"):
        _recovery(ResumeReconcilePolicy(), RecordingHaltPort())


def test_flatten_halt_requires_a_cancel_port() -> None:
    with pytest.raises(RecoveryError, match="OrderCancelPort"):
        _recovery(FlattenHaltPolicy(), RecordingHaltPort())


def test_policies_reject_ports_they_do_not_own() -> None:
    with pytest.raises(RecoveryError, match="does not accept a reconciler"):
        _recovery(
            HaltOnlyPolicy(),
            RecordingHaltPort(),
            reconciler=_reconciler(_clean_state(_hub({"alpha": []}))),
        )
    with pytest.raises(RecoveryError, match="does not accept a cancel port"):
        _recovery(
            ResumeReconcilePolicy(),
            RecordingHaltPort(),
            cancel=RecordingCancelPort(),
            reconciler=_reconciler(_clean_state(_hub({"alpha": []}))),
        )
    with pytest.raises(RecoveryError, match="does not accept a reconciler"):
        _recovery(
            FlattenHaltPolicy(),
            RecordingHaltPort(),
            cancel=RecordingCancelPort(),
            reconciler=_reconciler(_clean_state(_hub({"alpha": []}))),
        )


def test_reconciler_run_mismatch_is_rejected() -> None:
    reconciler = RuntimeReconciler(
        broker=FakeBrokerStatePort(_clean_state(_hub({"alpha": []}))),
        run_id="other-run",
    )
    with pytest.raises(RecoveryError, match="does not match recovery run"):
        _recovery(ResumeReconcilePolicy(), RecordingHaltPort(), reconciler=reconciler)


def test_recover_validates_run_binding_and_inputs() -> None:
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(),
        halt,
        reconciler=_reconciler(_clean_state(_hub({"alpha": []}))),
    )
    with pytest.raises(RecoveryError, match="does not match recovery run"):
        recovery.recover(store=RuntimeStateStore(MemorySnapshotStore(), run_id="x"))
    fills = [_fill(1)]
    foreign = _snapshot(_hub({"alpha": fills}), fills, run_id="other-run")
    with pytest.raises(RecoveryError, match="does not match recovery run"):
        recovery.recover(snapshot=foreign)
    with pytest.raises(ValueError, match="timezone-aware"):
        recovery.recover(store=_store(), event_time=datetime(2024, 1, 2, 15, 0))
    with pytest.raises(ValueError, match="must not be negative"):
        recovery.recover(store=_store(), reconcile_sequence=-1)


# --------------------------------------------------------------------------
# Resume-reconcile: clean boot continues dispatch
# --------------------------------------------------------------------------


def test_resume_reconcile_continues_on_clean_reconciliation() -> None:
    fills = [_fill(1), _fill(2, side=Side.SELL, quantity=Decimal("4"))]
    hub = _hub({"alpha": fills})
    store = _store()
    snapshot = _snapshot(hub, fills)
    store.save(snapshot)
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(), halt, reconciler=_reconciler(_clean_state(hub))
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.RESUME
    assert report.failure is None
    assert report.policy is BootPolicyKind.RESUME_RECONCILE
    assert report.run_id == RUN_ID
    assert report.snapshot_id == snapshot.snapshot_id
    assert report.event_time == EVENT_TIME
    assert _phases(report) == (
        RecoveryPhase.SNAPSHOT_LOAD,
        RecoveryPhase.STATE_RESTORE,
        RecoveryPhase.RECONCILE,
        RecoveryPhase.RESUME,
    )
    assert report.reconcile is not None
    assert report.reconcile.is_clean
    assert report.reconcile.decision is ReconcileDecision.PROCEED
    assert report.reconcile.trigger is ReconcileTrigger.BOOT
    assert report.hub is not None
    assert report.hub.snapshot == hub.snapshot
    assert report.randoms == {}
    assert report.strategy_state == {}
    assert halt.reasons == []


def test_resume_reconcile_adopts_injected_broker_divergence() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    store = _store()
    store.save(_snapshot(hub, fills))
    divergent = BrokerStateSnapshot(
        account=BrokerAccountRecord("acct-1", hub.snapshot.cash + Decimal("100")),
        positions=(
            BrokerPositionRecord("AAPL", Decimal("10"), Decimal("50")),
            BrokerPositionRecord("MSFT", Decimal("5"), Decimal("30")),
        ),
    )
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(),
        halt,
        reconciler=_reconciler(
            divergent, policy=StaticPolicy(ReconcileDecision.PROCEED)
        ),
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.RESUME
    assert report.failure is None
    assert report.reconcile is not None
    assert report.reconcile.decision is ReconcileDecision.PROCEED
    assert not report.reconcile.is_clean
    (missing,) = report.reconcile.diff.positions_missing
    assert missing.symbol == "MSFT"
    assert missing.broker_quantity == Decimal("5")
    cash = report.reconcile.diff.cash
    assert cash is not None
    assert cash.within_tolerance is False
    envelope = json.loads(report.reconcile.artifact_bytes)
    assert envelope["diff_id"] == report.reconcile.diff_id
    assert envelope["diff"]["positions_missing"][0]["symbol"] == "MSFT"
    assert envelope["diff"]["cash"]["within_tolerance"] is False
    assert _phases(report)[-1] is RecoveryPhase.RESUME
    assert halt.reasons == []


def test_resume_reconcile_halts_on_divergence_under_default_policy() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    store = _store()
    store.save(_snapshot(hub, fills))
    divergent = BrokerStateSnapshot(
        account=BrokerAccountRecord("acct-1", hub.snapshot.cash + Decimal("100")),
        positions=(BrokerPositionRecord("AAPL", Decimal("10"), Decimal("50")),),
    )
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(), halt, reconciler=_reconciler(divergent)
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is None
    assert report.reconcile is not None
    assert report.reconcile.decision is ReconcileDecision.ABORT
    cash = report.reconcile.diff.cash
    assert cash is not None
    assert cash.delta == Decimal("100")
    envelope = json.loads(report.reconcile.artifact_bytes)
    assert envelope["diff"]["cash"]["delta"] == {"$decimal": "100"}
    assert _phases(report)[-1] is RecoveryPhase.HALT
    assert halt.reasons == ["recovery:resume_reconcile:abort"]


# --------------------------------------------------------------------------
# Chaos taxonomy: every fault deterministically halts
# --------------------------------------------------------------------------


def test_missing_snapshot_halts_deterministically() -> None:
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(), halt, reconciler=_reconciler(_clean_state(_hub({})))
    )

    report = recovery.recover(store=_store(), event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "snapshot load failed" in report.failure
    assert _phases(report) == (RecoveryPhase.SNAPSHOT_LOAD, RecoveryPhase.HALT)
    assert report.reconcile is None
    assert halt.reasons == ["recovery:snapshot_load_failed"]


def test_torn_snapshot_halts_deterministically() -> None:
    content = MemorySnapshotStore()
    store = _store(content)
    fills = [_fill(1)]
    snapshot = _snapshot(_hub({"alpha": fills}), fills)
    store.save(snapshot)
    payload_key = f"runs/{RUN_ID}/snapshots/{snapshot.snapshot_id}.json"
    payload = bytearray(content.read(payload_key))
    payload[0] ^= 0xFF
    content.replace(payload_key, bytes(payload))
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(), halt, reconciler=_reconciler(_clean_state(_hub({})))
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "snapshot load failed" in report.failure
    assert halt.reasons == ["recovery:snapshot_load_failed"]


def test_broker_unavailable_mid_reconcile_halts() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    store = _store()
    store.save(_snapshot(hub, fills))
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(),
        halt,
        reconciler=_reconciler(OSError("broker session down")),
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "boot reconciliation failed" in report.failure
    assert "broker session down" in report.failure
    assert _phases(report) == (
        RecoveryPhase.SNAPSHOT_LOAD,
        RecoveryPhase.STATE_RESTORE,
        RecoveryPhase.RECONCILE,
        RecoveryPhase.HALT,
    )
    assert halt.reasons == ["recovery:reconcile_failed"]


def test_conflicting_marks_across_books_halt_at_restore() -> None:
    store = _store()
    snapshot = RuntimeSnapshot(
        format_version=FORMAT_VERSION,
        run_id=RUN_ID,
        trigger=SnapshotTrigger.STOP,
        sequence=0,
        dispatched_count=0,
        market_event_count=0,
        options={"run_id": RUN_ID, "initial_cash": CASH},
        books=(
            BookState(
                book_id="alpha",
                initial_cash=CASH,
                cash=CASH,
                fill_ids=(),
                fills=(),
                markers=(PositionMarker(Symbol("AAPL"), Decimal("50")),),
            ),
            BookState(
                book_id="beta",
                initial_cash=CASH,
                cash=CASH,
                fill_ids=(),
                fills=(),
                markers=(PositionMarker(Symbol("AAPL"), Decimal("51")),),
            ),
        ),
        prices={},
        order_counts={},
        order_owners={},
        strategy_state=(),
        random_states=(),
        risk=RiskState(on_reject="reject_order", rule_ids=(), daily_losses={}),
    )
    store.save(snapshot)
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(), halt, reconciler=_reconciler(_clean_state(_hub({})))
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "state restore failed" in report.failure
    assert halt.reasons == ["recovery:state_restore_failed"]


def test_cancel_fault_stops_the_sequence_before_flattening() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    cancel = RecordingCancelPort(fail_on=frozenset({"o-2"}))
    halt = RecordingHaltPort()
    recovery = _recovery(FlattenHaltPolicy(), halt, cancel=cancel)

    report = recovery.recover(
        hub=hub,
        open_order_ids=["o-2", "o-1"],
        marks={"AAPL": Decimal("52")},
        event_time=EVENT_TIME,
    )

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "cancel-all incomplete" in report.failure
    assert cancel.calls == ["o-1"]
    assert report.cancels is not None
    assert report.cancels.failed == "o-2"
    assert report.cancels.complete is False
    assert report.flatten is None
    assert hub.book("alpha").position("AAPL").quantity == Decimal("10")
    assert _phases(report)[-1] is RecoveryPhase.HALT
    assert halt.reasons == ["recovery:cancel_all_incomplete"]


def test_flatten_without_a_mark_halts_without_fills() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    halt = RecordingHaltPort()
    recovery = _recovery(FlattenHaltPolicy(), halt, cancel=RecordingCancelPort())

    report = recovery.recover(hub=hub, open_order_ids=[], event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "no mark supplied" in report.failure
    assert report.flatten is None
    assert hub.book("alpha").position("AAPL").quantity == Decimal("10")
    assert halt.reasons == ["recovery:flatten_failed"]


def test_halt_mechanism_fault_is_still_a_recorded_halt() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    halt = RecordingHaltPort()
    halt.fail = True
    recovery = _recovery(HaltOnlyPolicy(), halt)

    report = recovery.recover(hub=hub, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "halt mechanism failed" in report.failure
    assert _phases(report)[-1] is RecoveryPhase.HALT


# --------------------------------------------------------------------------
# Cancel-all sequencing: deterministic order, idempotent
# --------------------------------------------------------------------------


def test_cancel_all_commands_a_deduplicated_sorted_sequence() -> None:
    port = RecordingCancelPort()
    outcome = cancel_all(port, ["o-b", "o-a", "o-a", "o-c"])
    assert outcome.requested == ("o-a", "o-b", "o-c")
    assert outcome.canceled == ("o-a", "o-b", "o-c")
    assert outcome.skipped == ()
    assert outcome.failed is None
    assert outcome.complete is True
    assert port.calls == ["o-a", "o-b", "o-c"]


def test_cancel_all_skips_already_canceled_identifiers() -> None:
    port = RecordingCancelPort()
    outcome = cancel_all(port, ["o-a", "o-b"], already_canceled={"o-a"})
    assert outcome.requested == ("o-b",)
    assert outcome.skipped == ("o-a",)
    assert port.calls == ["o-b"]


def test_cancel_all_reports_the_first_failing_command() -> None:
    port = RecordingCancelPort(fail_on=frozenset({"o-b"}))
    outcome = cancel_all(port, ["o-c", "o-b", "o-a"])
    assert outcome.canceled == ("o-a",)
    assert outcome.failed == "o-b"
    assert outcome.complete is False
    assert port.calls == ["o-a"]


def test_flatten_halt_is_idempotent_across_retried_passes() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    cancel = RecordingCancelPort(strict=True)
    halt = RecordingHaltPort()
    recovery = _recovery(FlattenHaltPolicy(), halt, cancel=cancel)
    orders = ["w-2", "a-1"]

    first = recovery.recover(
        hub=hub,
        open_order_ids=orders,
        marks={"AAPL": Decimal("52")},
        event_time=EVENT_TIME,
    )
    second = recovery.recover(
        hub=hub,
        open_order_ids=orders,
        marks={"AAPL": Decimal("52")},
        event_time=EVENT_TIME,
    )

    assert cancel.calls == ["a-1", "w-2"]
    assert first.cancels is not None
    assert first.cancels.canceled == ("a-1", "w-2")
    assert second.cancels is not None
    assert second.cancels.requested == ()
    assert second.cancels.skipped == ("a-1", "w-2")
    assert first.flatten is not None
    assert second.flatten is not None
    assert len(first.flatten.fills) == 1
    assert second.flatten.fills == ()
    assert second.flatten.flat is True
    assert first.action is RecoveryAction.HALT
    assert second.action is RecoveryAction.HALT
    assert halt.reasons == ["recovery:flatten_halt", "recovery:flatten_halt"]


# --------------------------------------------------------------------------
# Flatten-halt: positions end in cash, halt dispatch
# --------------------------------------------------------------------------


def test_flatten_halt_flattens_positions_to_cash_and_halts() -> None:
    hub = _hub(
        {
            "alpha": [_fill(1)],
            "beta": [
                _fill(2, side=Side.SELL, quantity=Decimal("4"), price=Decimal("70"))
            ],
        }
    )
    cancel = RecordingCancelPort()
    halt = RecordingHaltPort()
    recovery = _recovery(FlattenHaltPolicy(), halt, cancel=cancel)

    report = recovery.recover(
        hub=hub,
        open_order_ids=["z-1", "a-1"],
        marks={"AAPL": Decimal("52")},
        event_time=EVENT_TIME,
        base_sequence=100,
    )

    assert report.action is RecoveryAction.HALT
    assert report.failure is None
    assert cancel.calls == ["a-1", "z-1"]
    assert report.flatten is not None
    assert report.flatten.books == ("alpha", "beta")
    assert report.flatten.flat is True
    assert [fill.fill_id for fill in report.flatten.fills] == [
        f"{RUN_ID}-flatten-alpha-AAPL",
        f"{RUN_ID}-flatten-beta-AAPL",
    ]
    assert [fill.sequence for fill in report.flatten.fills] == [101, 102]
    assert report.flatten.liquidated_value == Decimal("728")
    assert report.flatten.fees == ZERO
    for book in hub.books:
        for position in book.snapshot.positions:
            assert position.quantity == ZERO
    assert hub.book("alpha").cash == Decimal("100020")
    assert hub.book("beta").cash == Decimal("100072")
    assert _phases(report) == (
        RecoveryPhase.CANCEL_ALL,
        RecoveryPhase.FLATTEN,
        RecoveryPhase.HALT,
    )
    assert halt.reasons == ["recovery:flatten_halt"]


def test_flatten_uses_snapshot_prices_and_honors_explicit_marks() -> None:
    fills = [_fill(1, quantity=Decimal("5"), price=Decimal("40"))]
    hub = _hub({"alpha": fills})
    store = _store()
    store.save(_snapshot(hub, fills, prices={"AAPL": Decimal("41")}))
    halt = RecordingHaltPort()
    recovery = _recovery(FlattenHaltPolicy(), halt, cancel=RecordingCancelPort())

    implicit = recovery.recover(store=store, event_time=EVENT_TIME)
    assert implicit.flatten is not None
    assert implicit.flatten.fills[0].price == Decimal("41")
    assert implicit.hub is not None
    assert implicit.hub.book("alpha").cash == Decimal("100000") - Decimal(
        "200"
    ) + Decimal("205")

    explicit = recovery.recover(
        store=store, marks={"AAPL": Decimal("43")}, event_time=EVENT_TIME
    )
    assert explicit.flatten is not None
    assert explicit.flatten.fills[0].price == Decimal("43")


def test_flatten_positions_is_deterministic_and_idempotent() -> None:
    fills = [_fill(1), _fill(2, side=Side.SELL, quantity=Decimal("4"))]
    hub_a = _hub({"alpha": fills})
    hub_b = _hub({"alpha": fills})
    first = flatten_positions(
        hub_a,
        marks={"AAPL": Decimal("52")},
        run_id=RUN_ID,
        event_time=EVENT_TIME,
        base_sequence=10,
    )
    second = flatten_positions(
        hub_b,
        marks={"AAPL": Decimal("52")},
        run_id=RUN_ID,
        event_time=EVENT_TIME,
        base_sequence=10,
    )
    assert first == second
    assert first.to_json() == second.to_json()
    retry = flatten_positions(
        hub_a,
        marks={"AAPL": Decimal("52")},
        run_id=RUN_ID,
        event_time=EVENT_TIME,
        base_sequence=10,
    )
    assert retry.fills == ()
    assert retry.books == ()
    assert retry.flat is True

    with pytest.raises(TypeError, match="event_time"):
        flatten_positions(
            _hub({"alpha": fills}),
            marks={"AAPL": Decimal("52")},
            run_id=RUN_ID,
            event_time=None,
            base_sequence=0,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        flatten_positions(
            _hub({"alpha": fills}),
            marks={"AAPL": Decimal("52")},
            run_id=RUN_ID,
            event_time=datetime(2024, 1, 2, 15, 0),
            base_sequence=0,
        )


# --------------------------------------------------------------------------
# Halt-only: preserve state, halt dispatch
# --------------------------------------------------------------------------


def test_halt_only_preserves_state_and_halts_dispatch() -> None:
    fills = [_fill(1)]
    hub = _hub({"alpha": fills})
    before = hub.snapshot
    store = _store()
    snapshot = _snapshot(hub, fills)
    store.save(snapshot)
    halt = RecordingHaltPort()
    recovery = _recovery(HaltOnlyPolicy(), halt)

    report = recovery.recover(
        hub=hub,
        store=store,
        open_order_ids=["o-1"],
        event_time=EVENT_TIME,
    )

    assert report.action is RecoveryAction.HALT
    assert report.failure is None
    assert report.policy is BootPolicyKind.HALT_ONLY
    assert report.snapshot_id == snapshot.snapshot_id
    assert hub.snapshot == before
    assert report.cancels is None
    assert report.flatten is None
    assert report.reconcile is None
    assert _phases(report) == (
        RecoveryPhase.SNAPSHOT_LOAD,
        RecoveryPhase.STATE_PRESERVED,
        RecoveryPhase.HALT,
    )
    assert halt.reasons == ["recovery:halt_only"]


def test_halt_only_without_a_stored_pointer_still_halts() -> None:
    halt = RecordingHaltPort()
    recovery = _recovery(HaltOnlyPolicy(), halt)

    report = recovery.recover(store=_store(), event_time=EVENT_TIME)

    assert report.action is RecoveryAction.HALT
    assert report.failure is not None
    assert "snapshot pointer unavailable" in report.failure
    assert report.snapshot_id is None
    assert halt.reasons == ["recovery:halt_only"]


def test_kill_switch_halt_port_trips_an_audited_switch() -> None:
    switch = KillSwitch()
    port = KillSwitchHaltPort(switch)

    port.halt(reason="recovery:halt_only")

    assert switch.engaged
    assert switch.last_event is not None
    assert switch.last_event.action is KillSwitchAction.TRIP
    assert switch.last_event.reason == "recovery:halt_only"
    assert switch.last_event.actor == "runtime-recovery"
    with pytest.raises(TypeError, match="KillSwitch"):
        KillSwitchHaltPort(switch=cast("KillSwitch", object()))


# --------------------------------------------------------------------------
# Report auditability
# --------------------------------------------------------------------------


def test_report_identity_is_canonical_and_stable() -> None:
    fills = [_fill(1)]

    def run() -> RecoveryReport:
        hub = _hub({"alpha": fills})
        store = _store()
        store.save(_snapshot(hub, fills))
        halt = RecordingHaltPort()
        recovery = _recovery(
            ResumeReconcilePolicy(), halt, reconciler=_reconciler(_clean_state(hub))
        )
        return recovery.recover(store=store, event_time=EVENT_TIME)

    first = run()
    second = run()
    assert first.report_id == second.report_id
    assert first.to_json() == second.to_json()
    content = canonical_configuration_bytes(first.to_json())
    assert first.report_id == hashlib.sha256(_REPORT_DOMAIN + content).hexdigest()

    halted = _recovery(HaltOnlyPolicy(), RecordingHaltPort()).recover(
        event_time=EVENT_TIME
    )
    assert halted.report_id != first.report_id


# --------------------------------------------------------------------------
# Randomized kill and restore cycles (acceptance AC1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cycle", range(20))
def test_randomized_kill_restore_cycles_reach_the_uninterrupted_end_state(
    cycle: int,
) -> None:
    rng = Random(2026_0913 + cycle * 7919)
    count = rng.randint(4, 10)
    kill = rng.randint(1, count - 1)
    fills = [
        _fill(
            index,
            symbol=rng.choice(("AAPL", "MSFT")),
            side=Side.BUY if rng.random() < 0.5 else Side.SELL,
            quantity=Decimal(rng.randint(1, 20)),
            price=Decimal(40 + rng.randint(0, 20)),
        )
        for index in range(1, count + 1)
    ]
    reference = _hub({"alpha": fills})
    killed = _hub({"alpha": fills[:kill]})
    store = _store()
    store.save(_snapshot(killed, fills[:kill]))
    halt = RecordingHaltPort()
    recovery = _recovery(
        ResumeReconcilePolicy(), halt, reconciler=_reconciler(_clean_state(killed))
    )

    report = recovery.recover(store=store, event_time=EVENT_TIME)

    assert report.action is RecoveryAction.RESUME
    assert report.failure is None
    assert report.reconcile is not None
    assert report.reconcile.is_clean
    assert report.reconcile.decision is ReconcileDecision.PROCEED
    assert halt.reasons == []
    assert report.hub is not None
    resumed = report.hub
    for fill in fills[kill:]:
        resumed.apply_fill("alpha", fill)
    assert resumed.snapshot == reference.snapshot
    assert resumed.book("alpha").fill_ids == reference.book("alpha").fill_ids
