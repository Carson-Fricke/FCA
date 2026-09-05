"""Continuous and boot reconciler behavior tests.

The tests prove the acceptance behaviors of the runtime reconciler:
divergence injected into the broker port triggers the configured policy at
the next scheduled interval, clean comparisons produce empty, canonically
hashed diff artifacts that boot and resume reconciliation classify through
the same implementation, and policy decisions are explicit and audited.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.symbols import Symbol
from harness.core.types import Fill, OrderStatus, Side
from harness.portfolio import PortfolioHub
from harness.runtime.reconciler import (
    FORMAT_VERSION,
    BrokerAccountRecord,
    BrokerOrderRecord,
    BrokerPositionRecord,
    BrokerStatePort,
    BrokerStateSnapshot,
    CashDelta,
    DefaultHaltPolicy,
    DiffArtifactStore,
    ExpectedOrderRecord,
    HaltOnDivergencePolicy,
    ReconcileDecision,
    ReconcilerError,
    ReconcileSchedule,
    ReconcileTrigger,
    ReconciliationBrokerError,
    ReconciliationDiff,
    ReconciliationOutcome,
    ReconciliationStoreError,
    ReconciliationTolerances,
    RuntimeReconciler,
    StaticPolicy,
)

_NOW = datetime(2024, 1, 2, 15, 30, tzinfo=UTC)
_RUN_ID = "paper-reconcile-1"
_INITIAL_CASH = Decimal("100000")
_DIFF_DOMAIN = b"fca:harness-runtime-reconcile-diff:v1\x00"


# --------------------------------------------------------------------------
# Deterministic fakes
# --------------------------------------------------------------------------


class FakeBrokerStatePort:
    """Deterministic broker port serving one injectable state snapshot."""

    def __init__(self, state: BrokerStateSnapshot) -> None:
        self.state = state
        self.calls = 0

    def fetch_state(self) -> BrokerStateSnapshot:
        self.calls += 1
        return self.state


class RecordingStore:
    """In-memory artifact store recording every whole-object replacement."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.replaced: list[str] = []

    def replace(self, relative_path: str, payload: bytes) -> None:
        self.objects[relative_path] = payload
        self.replaced.append(relative_path)

    def read(self, relative_path: str) -> bytes:
        return self.objects[relative_path]


class TamperingStore(RecordingStore):
    """Store whose read-back returns different bytes than were written."""

    def read(self, relative_path: str) -> bytes:
        return b"{}"


class FailingStore(RecordingStore):
    """Store whose underlying object write raises an OSError."""

    def replace(self, relative_path: str, payload: bytes) -> None:
        raise OSError("blob store unavailable")


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _hub(
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: Decimal = Decimal("100"),
    price: Decimal = Decimal("50"),
) -> PortfolioHub:
    hub = PortfolioHub(_RUN_ID)
    hub.create_book("alpha", _INITIAL_CASH)
    hub.apply_fill(
        "alpha",
        Fill(
            event_id="evt-1",
            timestamp=_NOW,
            sequence=1,
            fill_id="fill-1",
            order_id="order-1",
            symbol=Symbol(symbol),
            side=side,
            quantity=quantity,
            price=price,
        ),
    )
    return hub


def _state(
    *,
    cash: Decimal = Decimal("95000"),
    positions: tuple[BrokerPositionRecord, ...] = (
        BrokerPositionRecord("AAPL", Decimal("100"), Decimal("50")),
    ),
    orders: tuple[BrokerOrderRecord, ...] = (),
) -> BrokerStateSnapshot:
    return BrokerStateSnapshot(
        account=BrokerAccountRecord("acct-1", cash),
        positions=positions,
        orders=orders,
    )


def _expected(
    *,
    order_id: str = "order-2",
    symbol: str = "MSFT",
    status: OrderStatus = OrderStatus.ACCEPTED,
    quantity: Decimal = Decimal("10"),
    filled: Decimal = Decimal("0"),
) -> ExpectedOrderRecord:
    return ExpectedOrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=quantity,
        filled_quantity=filled,
        status=status,
    )


def _reconciler(
    state: BrokerStateSnapshot,
    *,
    policy: HaltOnDivergencePolicy | StaticPolicy | None = None,
    tolerances: ReconciliationTolerances | None = None,
    schedule: ReconcileSchedule | None = None,
    store: DiffArtifactStore | None = None,
) -> RuntimeReconciler:
    return RuntimeReconciler(
        broker=FakeBrokerStatePort(state),
        run_id=_RUN_ID,
        policy=policy,
        tolerances=tolerances,
        schedule=schedule,
        store=store,
    )


def _reconcile(
    reconciler: RuntimeReconciler,
    hub: PortfolioHub,
    *,
    trigger: ReconcileTrigger = ReconcileTrigger.PERIODIC,
    sequence: int = 1,
    expected_orders: tuple[ExpectedOrderRecord, ...] = (),
    observed_at: datetime | None = None,
) -> ReconciliationOutcome:
    return reconciler.reconcile(
        hub=hub,
        trigger=trigger,
        sequence=sequence,
        expected_orders=expected_orders,
        observed_at=observed_at,
    )


# --------------------------------------------------------------------------
# Construction and validation
# --------------------------------------------------------------------------


def test_reconciler_requires_a_broker_state_port() -> None:
    with pytest.raises(TypeError, match="BrokerStatePort"):
        RuntimeReconciler(broker=cast("BrokerStatePort", object()), run_id=_RUN_ID)


def test_reconciler_binds_to_a_safe_run_identifier() -> None:
    reconciler = _reconciler(_state())
    assert reconciler.run_id == _RUN_ID
    with pytest.raises(ValueError, match="run identifier"):
        RuntimeReconciler(broker=FakeBrokerStatePort(_state()), run_id="../escape")


def test_reconcile_rejects_non_advancing_sequences() -> None:
    reconciler = _reconciler(_state())
    hub = _hub()
    _reconcile(reconciler, hub, sequence=3)
    with pytest.raises(ReconcilerError, match="does not advance"):
        _reconcile(reconciler, hub, sequence=3)
    with pytest.raises(ReconcilerError, match="does not advance"):
        _reconcile(reconciler, hub, sequence=2)
    with pytest.raises(ValueError, match="sequence"):
        _reconcile(reconciler, hub, sequence=-1)


def test_reconcile_rejects_duplicate_expected_order_identities() -> None:
    reconciler = _reconciler(_state())
    with pytest.raises(ReconcilerError, match="repeat an order identity"):
        _reconcile(
            reconciler,
            _hub(),
            expected_orders=(_expected(), _expected()),
        )


def test_broker_port_must_return_a_snapshot() -> None:
    class BadPort:
        def fetch_state(self) -> dict[str, object]:
            return {"cash": "95000"}

    reconciler = RuntimeReconciler(
        broker=cast("BrokerStatePort", BadPort()), run_id=_RUN_ID
    )
    with pytest.raises(ReconciliationBrokerError, match="BrokerStateSnapshot"):
        _reconcile(reconciler, _hub())


def test_ports_satisfy_the_runtime_checkable_protocols() -> None:
    assert isinstance(FakeBrokerStatePort(_state()), BrokerStatePort)
    assert isinstance(RecordingStore(), DiffArtifactStore)
    assert DefaultHaltPolicy is HaltOnDivergencePolicy


# --------------------------------------------------------------------------
# Clean comparisons and empty diff artifacts
# --------------------------------------------------------------------------


def test_clean_comparison_produces_empty_diff_and_proceeds() -> None:
    reconciler = _reconciler(_state())
    outcome = _reconcile(reconciler, _hub(), observed_at=_NOW)
    assert outcome.is_clean
    assert outcome.diff.is_empty
    assert outcome.diff.diff_id == outcome.diff_id
    assert outcome.decision is ReconcileDecision.PROCEED
    assert outcome.decision_source == "HaltOnDivergencePolicy"


def test_clean_comparison_persists_an_empty_diff_artifact() -> None:
    store = RecordingStore()
    reconciler = _reconciler(_state(), store=store)
    outcome = _reconcile(reconciler, _hub(), sequence=7)
    key = "runs/paper-reconcile-1/reconcile/00000000000000000007.json"
    assert store.replaced == [key]
    envelope = json.loads(store.objects[key])
    assert envelope["diff_id"] == outcome.diff_id
    assert envelope["diff"]["format_version"] == FORMAT_VERSION
    assert envelope["diff"]["cash"]["within_tolerance"] is True
    for field_name in (
        "positions_missing",
        "positions_extra",
        "positions_mismatched",
        "orders_missing",
        "orders_unexpected",
        "orders_drifted",
    ):
        assert envelope["diff"][field_name] == []
    assert outcome.artifact_bytes == store.objects[key]


def test_flat_and_zero_positions_are_ignored_on_both_sides() -> None:
    hub = _hub(quantity=Decimal("100"))
    reconciler = _reconciler(
        _state(
            positions=(
                BrokerPositionRecord("AAPL", Decimal("100"), Decimal("50")),
                BrokerPositionRecord("MSFT", Decimal("0"), Decimal("10")),
            )
        )
    )
    outcome = _reconcile(reconciler, hub)
    assert outcome.is_clean


def test_boot_and_periodic_triggers_share_one_implementation() -> None:
    hub = _hub()
    boot = _reconciler(_state())
    periodic = _reconciler(_state())
    boot_outcome = _reconcile(boot, hub, trigger=ReconcileTrigger.BOOT, sequence=0)
    periodic_outcome = _reconcile(periodic, hub, sequence=1)
    assert boot_outcome.diff_id == periodic_outcome.diff_id
    assert boot_outcome.trigger is ReconcileTrigger.BOOT
    assert periodic_outcome.trigger is ReconcileTrigger.PERIODIC


# --------------------------------------------------------------------------
# Position classification
# --------------------------------------------------------------------------


def test_broker_only_position_is_classified_missing() -> None:
    state = _state(
        positions=(
            BrokerPositionRecord("AAPL", Decimal("100"), Decimal("50")),
            BrokerPositionRecord("MSFT", Decimal("25"), Decimal("10")),
        )
    )
    reconciler = _reconciler(state)
    outcome = _reconcile(reconciler, _hub())
    assert not outcome.is_clean
    assert outcome.decision is ReconcileDecision.ABORT
    (missing,) = outcome.diff.positions_missing
    assert missing.symbol == "MSFT"
    assert missing.broker_quantity == Decimal("25")
    assert missing.virtual_quantity is None
    assert missing.quantity_delta is None


def test_virtual_only_position_is_classified_extra() -> None:
    hub = _hub(symbol="MSFT", quantity=Decimal("25"), price=Decimal("10"))
    reconciler = _reconciler(_state())
    outcome = _reconcile(reconciler, hub)
    (extra,) = outcome.diff.positions_extra
    assert extra.symbol == "MSFT"
    assert extra.broker_quantity is None
    assert extra.virtual_quantity == Decimal("25")


def test_quantity_and_price_mismatches_carry_their_fields() -> None:
    state = _state(
        positions=(BrokerPositionRecord("AAPL", Decimal("90"), Decimal("50.5")),)
    )
    reconciler = _reconciler(state)
    outcome = _reconcile(reconciler, _hub())
    (mismatched,) = outcome.diff.positions_mismatched
    assert mismatched.symbol == "AAPL"
    assert mismatched.quantity_delta == Decimal("-10")
    assert mismatched.broker_average_price == Decimal("50.5")
    assert mismatched.virtual_average_price == Decimal("50")
    assert mismatched.mismatched_fields == ("quantity", "average_price")


def test_short_virtual_position_matches_a_broker_short() -> None:
    hub = _hub(side=Side.SELL, quantity=Decimal("30"), price=Decimal("20"))
    state = _state(
        cash=Decimal("100600"),
        positions=(BrokerPositionRecord("AAPL", Decimal("-30"), Decimal("20")),),
    )
    reconciler = _reconciler(state)
    outcome = _reconcile(reconciler, hub)
    assert outcome.is_clean


# --------------------------------------------------------------------------
# Tolerance boundaries
# --------------------------------------------------------------------------


def test_position_tolerance_boundary_is_exact() -> None:
    tolerances = ReconciliationTolerances(position_quantity=Decimal("0.001"))
    at_boundary = _state(
        positions=(BrokerPositionRecord("AAPL", Decimal("100.001"), Decimal("50")),)
    )
    reconciler = _reconciler(at_boundary, tolerances=tolerances)
    outcome = _reconcile(reconciler, _hub())
    assert outcome.is_clean

    beyond_boundary = _state(
        positions=(BrokerPositionRecord("AAPL", Decimal("100.002"), Decimal("50")),)
    )
    reconciler = _reconciler(beyond_boundary, tolerances=tolerances)
    outcome = _reconcile(reconciler, _hub())
    (mismatched,) = outcome.diff.positions_mismatched
    assert mismatched.mismatched_fields == ("quantity",)


def test_price_tolerance_boundary_is_exact() -> None:
    tolerances = ReconciliationTolerances(average_price=Decimal("0.01"))
    reconciler = _reconciler(
        _state(
            positions=(BrokerPositionRecord("AAPL", Decimal("100"), Decimal("50.01")),)
        ),
        tolerances=tolerances,
    )
    outcome = _reconcile(reconciler, _hub())
    assert outcome.is_clean

    reconciler = _reconciler(
        _state(
            positions=(BrokerPositionRecord("AAPL", Decimal("100"), Decimal("50.02")),)
        ),
        tolerances=tolerances,
    )
    outcome = _reconcile(reconciler, _hub())
    (mismatched,) = outcome.diff.positions_mismatched
    assert mismatched.mismatched_fields == ("average_price",)


def test_cash_tolerance_boundary_is_exact() -> None:
    tolerances = ReconciliationTolerances(cash=Decimal("0.005"))
    reconciler = _reconciler(_state(cash=Decimal("95000.005")), tolerances=tolerances)
    outcome = _reconcile(reconciler, _hub())
    assert outcome.is_clean

    reconciler = _reconciler(_state(cash=Decimal("95000.01")), tolerances=tolerances)
    outcome = _reconcile(reconciler, _hub())
    assert not outcome.is_clean
    cash = outcome.diff.cash
    assert cash is not None
    assert cash.delta == Decimal("0.01")
    assert cash.within_tolerance is False
    assert cash.broker_cash == Decimal("95000.01")
    assert cash.virtual_cash == Decimal("95000")


def test_negative_tolerances_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        ReconciliationTolerances(cash=Decimal("-0.01"))


# --------------------------------------------------------------------------
# Order drift
# --------------------------------------------------------------------------


def test_expected_open_order_absent_from_broker_is_missing() -> None:
    reconciler = _reconciler(_state())
    outcome = _reconcile(reconciler, _hub(), expected_orders=(_expected(),))
    (missing,) = outcome.diff.orders_missing
    assert missing.order_id == "order-2"
    assert missing.expected_status == "accepted"
    assert missing.broker_status is None
    assert missing.expected_filled_quantity == Decimal("0")
    assert missing.broker_filled_quantity is None
    assert not outcome.is_clean


def test_broker_open_order_absent_from_books_is_unexpected() -> None:
    broker_order = BrokerOrderRecord(
        order_id="order-9",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("10"),
        filled_quantity=Decimal("0"),
        status=OrderStatus.ACCEPTED,
    )
    reconciler = _reconciler(_state(orders=(broker_order,)))
    outcome = _reconcile(reconciler, _hub())
    (unexpected,) = outcome.diff.orders_unexpected
    assert unexpected.order_id == "order-9"
    assert unexpected.broker_status == "accepted"
    assert unexpected.expected_status is None


def test_broker_terminal_status_is_order_drift() -> None:
    broker_order = BrokerOrderRecord(
        order_id="order-2",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        status=OrderStatus.FILLED,
    )
    reconciler = _reconciler(_state(orders=(broker_order,)))
    outcome = _reconcile(reconciler, _hub(), expected_orders=(_expected(),))
    (drifted,) = outcome.diff.orders_drifted
    assert drifted.drifted_fields == ("status",)
    assert drifted.broker_status == "filled"
    assert drifted.filled_quantity_delta == Decimal("10")


def test_open_status_and_filled_quantity_drift_are_classified() -> None:
    tolerances = ReconciliationTolerances(order_filled_quantity=Decimal("1"))
    broker_order = BrokerOrderRecord(
        order_id="order-2",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("10"),
        filled_quantity=Decimal("2"),
        status=OrderStatus.PARTIALLY_FILLED,
    )
    reconciler = _reconciler(_state(orders=(broker_order,)), tolerances=tolerances)
    outcome = _reconcile(
        reconciler, _hub(), expected_orders=(_expected(status=OrderStatus.ACCEPTED),)
    )
    (drifted,) = outcome.diff.orders_drifted
    assert drifted.drifted_fields == ("status", "filled_quantity")
    assert drifted.filled_quantity_delta == Decimal("2")

    broker_order = BrokerOrderRecord(
        order_id="order-2",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("10"),
        filled_quantity=Decimal("1"),
        status=OrderStatus.PARTIALLY_FILLED,
    )
    reconciler = _reconciler(_state(orders=(broker_order,)), tolerances=tolerances)
    outcome = _reconcile(
        reconciler,
        _hub(),
        expected_orders=(
            _expected(status=OrderStatus.PARTIALLY_FILLED, filled=Decimal("0.5")),
        ),
    )
    assert outcome.diff.orders_drifted == ()


def test_terminal_broker_orders_never_expected_are_ignored() -> None:
    broker_order = BrokerOrderRecord(
        order_id="order-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("100"),
        filled_quantity=Decimal("100"),
        status=OrderStatus.FILLED,
    )
    reconciler = _reconciler(_state(orders=(broker_order,)))
    outcome = _reconcile(reconciler, _hub())
    assert outcome.diff.orders_unexpected == ()
    assert outcome.is_clean


def test_order_quantity_drift_is_classified() -> None:
    broker_order = BrokerOrderRecord(
        order_id="order-2",
        symbol="MSFT",
        side=Side.BUY,
        quantity=Decimal("11"),
        filled_quantity=Decimal("0"),
        status=OrderStatus.ACCEPTED,
    )
    reconciler = _reconciler(_state(orders=(broker_order,)))
    outcome = _reconcile(reconciler, _hub(), expected_orders=(_expected(),))
    (drifted,) = outcome.diff.orders_drifted
    assert drifted.drifted_fields == ("quantity",)


# --------------------------------------------------------------------------
# Policy invocation and audit
# --------------------------------------------------------------------------


def test_divergence_triggers_the_selected_policy_within_one_interval() -> None:
    divergent = _state(cash=Decimal("90000"))
    reconciler = _reconciler(divergent, schedule=ReconcileSchedule(every_events=2))
    hub = _hub()
    detected = None
    for event_count in range(1, 5):
        if reconciler.due(event_count):
            detected = _reconcile(reconciler, hub, sequence=event_count)
            break
    assert detected is not None
    assert detected.sequence == 2
    assert detected.decision is ReconcileDecision.ABORT
    assert not detected.is_clean


def test_schedule_is_due_only_on_cadence_boundaries() -> None:
    schedule = ReconcileSchedule(every_events=3)
    assert not schedule.due(0)
    assert not schedule.due(2)
    assert schedule.due(3)
    assert not schedule.due(4)
    with pytest.raises(ValueError, match="every_events"):
        ReconcileSchedule(every_events=0)


def test_reconciler_without_schedule_is_never_due() -> None:
    reconciler = _reconciler(_state())
    assert reconciler.due(1) is False


def test_static_policy_records_explicit_decisions() -> None:
    divergent = _state(cash=Decimal("90000"))
    hub = _hub()

    logging_reconciler = _reconciler(
        divergent, policy=StaticPolicy(ReconcileDecision.LOG)
    )
    outcome = _reconcile(logging_reconciler, hub, sequence=1)
    assert outcome.decision is ReconcileDecision.LOG
    assert outcome.decision_source == "StaticPolicy"

    proceeding_reconciler = _reconciler(
        divergent, policy=StaticPolicy(ReconcileDecision.PROCEED)
    )
    outcome = _reconcile(proceeding_reconciler, hub, sequence=2)
    assert outcome.decision is ReconcileDecision.PROCEED
    assert not outcome.is_clean


def test_audit_log_accumulates_outcomes_in_order() -> None:
    reconciler = _reconciler(_state())
    hub = _hub()
    first = _reconcile(reconciler, hub, sequence=1, observed_at=_NOW)
    second = _reconcile(reconciler, hub, sequence=2)
    assert reconciler.audit_log == (first, second)
    assert reconciler.last_sequence == 2
    assert first.observed_at == _NOW
    assert second.observed_at is None


def test_naive_observed_at_is_rejected() -> None:
    reconciler = _reconciler(_state())
    with pytest.raises(ValueError, match="timezone-aware"):
        _reconcile(
            reconciler,
            _hub(),
            observed_at=datetime(2024, 1, 2, 15, 30),
        )


# --------------------------------------------------------------------------
# Canonical hashing and diff identity
# --------------------------------------------------------------------------


def test_equal_states_hash_equally_and_divergent_states_differ() -> None:
    hub = _hub()
    first = _reconciler(_state())
    second = _reconciler(_state())
    clean_id = _reconcile(first, hub, sequence=1).diff_id
    assert clean_id == _reconcile(second, hub, sequence=2).diff_id

    divergent = _reconciler(_state(cash=Decimal("90000")))
    assert _reconcile(divergent, hub, sequence=3).diff_id != clean_id


def test_diff_identity_is_domain_separated_content_hash() -> None:
    diff = ReconciliationDiff(
        cash=CashDelta(Decimal("1"), Decimal("0"), Decimal("1"), False)
    )
    content = canonical_configuration_bytes(diff.to_json())
    assert diff.diff_id == hashlib.sha256(_DIFF_DOMAIN + content).hexdigest()


def test_empty_diff_is_clean_for_every_surface() -> None:
    diff = ReconciliationDiff(
        cash=CashDelta(Decimal("0"), Decimal("0"), Decimal("0"), True)
    )
    assert diff.is_empty
    assert diff.positions_missing == ()
    assert diff.orders_drifted == ()


# --------------------------------------------------------------------------
# Artifact persistence
# --------------------------------------------------------------------------


def test_store_failure_raises_a_reconciliation_store_error() -> None:
    with pytest.raises(ReconciliationStoreError, match="could not be persisted"):
        _reconciler(_state(), store=FailingStore()).reconcile(
            hub=_hub(),
            trigger=ReconcileTrigger.BOOT,
            sequence=0,
        )


def test_tampered_artifact_read_back_raises() -> None:
    reconciler = _reconciler(_state(), store=TamperingStore())
    with pytest.raises(ReconciliationStoreError, match="did not persist verbatim"):
        _reconcile(reconciler, _hub())


def test_artifacts_without_a_store_do_not_persist_but_still_audited() -> None:
    reconciler = _reconciler(_state())
    outcome = _reconcile(reconciler, _hub())
    assert reconciler.audit_log == (outcome,)
