"""Continuous and boot-time reconciliation of broker state against virtual books.

The reconciler compares one broker-reported state snapshot — positions, open
orders, and account cash — against the frozen virtual-book aggregate exposed
by a :class:`~harness.portfolio.PortfolioHub` snapshot, classifies every
divergence into a structured, canonically hashed diff artifact, invokes the
configured :class:`ReconciliationPolicy`, and records the audited outcome.

Design constraints carried from the runtime contract:

- Determinism: reconciliation runs only on explicit triggers — boot or an
  engine-clocked periodic schedule over admitted market events.  Nothing in
  this module reads wall time, draws randomness, or performs network I/O;
  the broker-side state enters through the injected :class:`BrokerStatePort`
  and any timestamp on an artifact is an explicit engine-clocked argument.
- Exactness: every quantity, price, and cash value is an exact ``Decimal``
  and every tolerance is an exact non-negative ``Decimal`` threshold.  A
  delta exactly equal to its tolerance reconciles; one smallest representable
  step beyond it diverges.
- Auditability: diff identity is the domain-separated SHA-256 of the canonical
  serialization (the repository's ``harness.configuration.hashing``
  convention), each run's artifacts persist through the injected
  :class:`DiffArtifactStore` with verify-then-commit semantics, and every
  policy decision is recorded on the returned
  :class:`ReconciliationOutcome` and in the in-memory audit trail.
- Policies are explicit: ``proceed``, ``log``, and ``abort`` are decided by
  the configured policy object and recorded on the outcome.  Raising or
  halting the runtime on an ``abort`` decision remains with the runtime
  composition, not the kernel surface here.

The broker-side records mirror the kernel :class:`~harness.core.types.
accounting.Position` and order-report shapes so a live adapter — for example
one composing :meth:`AlpacaBroker.request_position_snapshot` with the inbox
event stream — can translate broker frames mechanically.  The
:class:`DiffArtifactStore` port structurally matches the state store's
``SnapshotObjectStore`` (whole-object replace/read), so the same lake storage
adapters plug in directly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.symbols import canonical_symbol
from harness.core.types.common import (
    exact_decimal,
    optional_positive,
    text,
    timestamp,
)
from harness.core.types.orders import OrderStatus, Side
from harness.portfolio import PortfolioHub, PortfolioSnapshot

FORMAT_VERSION: Final = 1

_DIFF_DOMAIN: Final = b"fca:harness-runtime-reconcile-diff:v1\0"
_ZERO: Final = Decimal("0")
_ID_CHARACTERS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_OPEN_ORDER_STATUSES: Final = frozenset(
    {
        OrderStatus.NEW.value,
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.CANCEL_PENDING.value,
    }
)


class ReconcileTrigger(StrEnum):
    """Deterministic reconciliation trigger points."""

    BOOT = "boot"
    PERIODIC = "periodic"


class ReconcileDecision(StrEnum):
    """Explicit, audited reconciliation policy outcomes."""

    PROCEED = "proceed"
    LOG = "log"
    ABORT = "abort"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ReconcilerError(RuntimeError):
    """Base class for runtime reconciliation failures."""


class ReconciliationBrokerError(ReconcilerError):
    """The injected broker port returned an invalid or malformed state."""


class ReconciliationStoreError(ReconcilerError):
    """A diff artifact could not be persisted verifiably."""


# --------------------------------------------------------------------------
# Tolerances
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationTolerances:
    """Exact non-negative Decimal thresholds for each comparison surface.

    A delta whose absolute value is exactly equal to the tolerance
    reconciles; a delta beyond it diverges.  Zero tolerances demand exact
    equality, which the kernel uses by default.
    """

    position_quantity: Decimal = _ZERO
    average_price: Decimal = _ZERO
    cash: Decimal = _ZERO
    order_quantity: Decimal = _ZERO
    order_filled_quantity: Decimal = _ZERO

    def __post_init__(self) -> None:
        for field_name in (
            "position_quantity",
            "average_price",
            "cash",
            "order_quantity",
            "order_filled_quantity",
        ):
            value = exact_decimal(getattr(self, field_name), field_name)
            if value < _ZERO:
                raise ValueError(f"{field_name} must not be negative")
            object.__setattr__(self, field_name, value)

    def within(self, field_name: str, delta: Decimal) -> bool:
        """Return whether one absolute delta is inside a named tolerance."""
        tolerance = exact_decimal(getattr(self, field_name), field_name)
        return abs(delta) <= tolerance


# --------------------------------------------------------------------------
# Broker-side records and port
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerPositionRecord:
    """One broker-reported account position."""

    symbol: str
    quantity: Decimal
    average_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "symbol",
            str(canonical_symbol(text(self.symbol, "symbol"))),
        )
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, "quantity"))
        object.__setattr__(
            self,
            "average_price",
            optional_positive(self.average_price, "average_price"),
        )

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready position record."""
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "average_price": self.average_price,
        }


@dataclass(frozen=True, slots=True)
class BrokerOrderRecord:
    """One broker-reported order with its wire status."""

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(
            self,
            "symbol",
            str(canonical_symbol(text(self.symbol, "symbol"))),
        )
        object.__setattr__(self, "side", Side(self.side))
        quantity = exact_decimal(self.quantity, "quantity")
        if quantity <= _ZERO:
            raise ValueError("quantity must be positive")
        filled = exact_decimal(self.filled_quantity, "filled_quantity")
        if filled < _ZERO:
            raise ValueError("filled_quantity must not be negative")
        if filled > quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "status", OrderStatus(self.status))

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready order record."""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class BrokerAccountRecord:
    """One broker-reported account cash position."""

    account_id: str
    cash: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", text(self.account_id, "account_id"))
        object.__setattr__(self, "cash", exact_decimal(self.cash, "cash"))

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready account record."""
        return {"account_id": self.account_id, "cash": self.cash}


@dataclass(frozen=True, slots=True)
class BrokerStateSnapshot:
    """One atomic broker-reported state observation."""

    account: BrokerAccountRecord
    positions: tuple[BrokerPositionRecord, ...] = ()
    orders: tuple[BrokerOrderRecord, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.account, BrokerAccountRecord):
            raise TypeError("account must be a BrokerAccountRecord")
        positions = tuple(self.positions)
        seen_symbols: set[str] = set()
        for position in positions:
            if not isinstance(position, BrokerPositionRecord):
                raise TypeError("positions must contain BrokerPositionRecord values")
            if position.symbol in seen_symbols:
                raise ValueError(
                    f"broker state repeats position for {position.symbol!r}"
                )
            seen_symbols.add(position.symbol)
        object.__setattr__(self, "positions", positions)
        orders = tuple(self.orders)
        seen_orders: set[str] = set()
        for order in orders:
            if not isinstance(order, BrokerOrderRecord):
                raise TypeError("orders must contain BrokerOrderRecord values")
            if order.order_id in seen_orders:
                raise ValueError(f"broker state repeats order {order.order_id!r}")
            seen_orders.add(order.order_id)
        object.__setattr__(self, "orders", orders)


@runtime_checkable
class BrokerStatePort(Protocol):
    """Injected boundary supplying one atomic broker-side state snapshot.

    A live adapter composes the venue command surface — for example
    :meth:`AlpacaBroker.request_position_snapshot` plus the inbox event
    stream — into one validated :class:`BrokerStateSnapshot`.  The port
    performs no retries and consults no clocks; the caller owns when the
    snapshot is taken.
    """

    def fetch_state(self) -> BrokerStateSnapshot:
        """Return one atomic broker-reported state observation."""
        ...


@dataclass(frozen=True, slots=True)
class ExpectedOrderRecord:
    """One kernel-side open order expectation for the virtual book state."""

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(
            self,
            "symbol",
            str(canonical_symbol(text(self.symbol, "symbol"))),
        )
        object.__setattr__(self, "side", Side(self.side))
        quantity = exact_decimal(self.quantity, "quantity")
        if quantity <= _ZERO:
            raise ValueError("quantity must be positive")
        filled = exact_decimal(self.filled_quantity, "filled_quantity")
        if filled < _ZERO:
            raise ValueError("filled_quantity must not be negative")
        if filled > quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        status = OrderStatus(self.status)
        if status.value not in _OPEN_ORDER_STATUSES:
            raise ValueError(
                f"expected order {self.order_id!r} must carry an open status, "
                f"not {status.value!r}"
            )
        object.__setattr__(self, "status", status)


# --------------------------------------------------------------------------
# Structured diff records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionDelta:
    """One classified position divergence for a single symbol."""

    symbol: str
    broker_quantity: Decimal | None
    virtual_quantity: Decimal | None
    quantity_delta: Decimal | None
    broker_average_price: Decimal | None
    virtual_average_price: Decimal | None
    mismatched_fields: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready position delta."""
        return {
            "symbol": self.symbol,
            "broker_quantity": self.broker_quantity,
            "virtual_quantity": self.virtual_quantity,
            "quantity_delta": self.quantity_delta,
            "broker_average_price": self.broker_average_price,
            "virtual_average_price": self.virtual_average_price,
            "mismatched_fields": list(self.mismatched_fields),
        }


@dataclass(frozen=True, slots=True)
class OrderDelta:
    """One classified order drift for a single order identity."""

    order_id: str
    expected_status: str | None
    broker_status: str | None
    expected_filled_quantity: Decimal | None
    broker_filled_quantity: Decimal | None
    filled_quantity_delta: Decimal | None
    drifted_fields: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready order delta."""
        return {
            "order_id": self.order_id,
            "expected_status": self.expected_status,
            "broker_status": self.broker_status,
            "expected_filled_quantity": self.expected_filled_quantity,
            "broker_filled_quantity": self.broker_filled_quantity,
            "filled_quantity_delta": self.filled_quantity_delta,
            "drifted_fields": list(self.drifted_fields),
        }


@dataclass(frozen=True, slots=True)
class CashDelta:
    """The exact broker-versus-virtual cash difference."""

    broker_cash: Decimal
    virtual_cash: Decimal
    delta: Decimal
    within_tolerance: bool

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready cash delta."""
        return {
            "broker_cash": self.broker_cash,
            "virtual_cash": self.virtual_cash,
            "delta": self.delta,
            "within_tolerance": self.within_tolerance,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationDiff:
    """Structured, canonically hashed classification of one comparison."""

    positions_missing: tuple[PositionDelta, ...] = ()
    positions_extra: tuple[PositionDelta, ...] = ()
    positions_mismatched: tuple[PositionDelta, ...] = ()
    orders_missing: tuple[OrderDelta, ...] = ()
    orders_unexpected: tuple[OrderDelta, ...] = ()
    orders_drifted: tuple[OrderDelta, ...] = ()
    cash: CashDelta | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "positions_missing",
            "positions_extra",
            "positions_mismatched",
        ):
            deltas = tuple(getattr(self, field_name))
            for delta in deltas:
                if not isinstance(delta, PositionDelta):
                    raise TypeError(f"{field_name} must contain PositionDelta values")
            object.__setattr__(self, field_name, deltas)
        for field_name in (
            "orders_missing",
            "orders_unexpected",
            "orders_drifted",
        ):
            deltas = tuple(getattr(self, field_name))
            for delta in deltas:
                if not isinstance(delta, OrderDelta):
                    raise TypeError(f"{field_name} must contain OrderDelta values")
            object.__setattr__(self, field_name, deltas)
        if self.cash is not None and not isinstance(self.cash, CashDelta):
            raise TypeError("cash must be a CashDelta")

    @property
    def is_empty(self) -> bool:
        """Whether every comparison surface reconciled within tolerance."""
        return not (
            self.positions_missing
            or self.positions_extra
            or self.positions_mismatched
            or self.orders_missing
            or self.orders_unexpected
            or self.orders_drifted
            or (self.cash is not None and not self.cash.within_tolerance)
        )

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready diff content."""
        return {
            "format_version": FORMAT_VERSION,
            "positions_missing": [item.to_json() for item in self.positions_missing],
            "positions_extra": [item.to_json() for item in self.positions_extra],
            "positions_mismatched": [
                item.to_json() for item in self.positions_mismatched
            ],
            "orders_missing": [item.to_json() for item in self.orders_missing],
            "orders_unexpected": [item.to_json() for item in self.orders_unexpected],
            "orders_drifted": [item.to_json() for item in self.orders_drifted],
            "cash": None if self.cash is None else self.cash.to_json(),
        }

    @property
    def diff_id(self) -> str:
        """Return the domain-separated SHA-256 identity of this diff."""
        return hashlib.sha256(
            _DIFF_DOMAIN + canonical_configuration_bytes(self.to_json())
        ).hexdigest()

    @property
    def artifact_bytes(self) -> bytes:
        """Return the canonical content-addressed artifact envelope."""
        return canonical_configuration_bytes(
            {
                "diff_id": self.diff_id,
                "diff": self.to_json(),
            }
        )


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------


@runtime_checkable
class ReconciliationPolicy(Protocol):
    """Configured decision boundary invoked for every reconciliation."""

    def decide(self, diff: ReconciliationDiff) -> ReconcileDecision:
        """Return the explicit decision for one classified diff."""
        ...


@dataclass(frozen=True, slots=True)
class StaticPolicy:
    """Return one fixed decision for every reconciliation."""

    decision: ReconcileDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", ReconcileDecision(self.decision))

    def decide(self, diff: ReconciliationDiff) -> ReconcileDecision:
        """Return the configured decision regardless of the diff."""
        return self.decision


@dataclass(frozen=True, slots=True)
class HaltOnDivergencePolicy:
    """The default policy: proceed when clean, abort on any divergence."""

    def decide(self, diff: ReconciliationDiff) -> ReconcileDecision:
        """Abort on any divergence beyond tolerance, else proceed."""
        return ReconcileDecision.PROCEED if diff.is_empty else ReconcileDecision.ABORT


DefaultHaltPolicy = HaltOnDivergencePolicy


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileSchedule:
    """Deterministic periodic cadence over admitted market events."""

    every_events: int

    def __post_init__(self) -> None:
        if isinstance(self.every_events, bool) or not isinstance(
            self.every_events, int
        ):
            raise ValueError("every_events must be an integer")
        if self.every_events <= 0:
            raise ValueError("every_events must be positive")

    def due(self, market_event_count: int) -> bool:
        """Return whether a periodic reconciliation is due after this many events."""
        if market_event_count < 0:
            raise ValueError("market_event_count must not be negative")
        return market_event_count > 0 and market_event_count % self.every_events == 0


# --------------------------------------------------------------------------
# Outcome
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """One audited reconciliation run: diff, decision, and provenance."""

    run_id: str
    trigger: ReconcileTrigger
    sequence: int
    observed_at: datetime | None
    diff: ReconciliationDiff
    decision: ReconcileDecision
    decision_source: str

    def __post_init__(self) -> None:
        text(self.run_id, "run_id")
        if not isinstance(self.trigger, ReconcileTrigger):
            raise TypeError("trigger must be a ReconcileTrigger")
        if not isinstance(self.diff, ReconciliationDiff):
            raise TypeError("diff must be a ReconciliationDiff")
        if not isinstance(self.decision, ReconcileDecision):
            raise TypeError("decision must be a ReconcileDecision")
        text(self.decision_source, "decision_source")

    @property
    def diff_id(self) -> str:
        """Return the content identity of the classified diff."""
        return self.diff.diff_id

    @property
    def is_clean(self) -> bool:
        """Whether the comparison produced no divergence."""
        return self.diff.is_empty

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready audit record."""
        return {
            "format_version": FORMAT_VERSION,
            "run_id": self.run_id,
            "trigger": self.trigger.value,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "diff_id": self.diff_id,
            "diff": self.diff.to_json(),
            "decision": self.decision.value,
            "decision_source": self.decision_source,
        }

    @property
    def artifact_bytes(self) -> bytes:
        """Return the canonical stored artifact envelope for this outcome."""
        return canonical_configuration_bytes(self.to_json())


@runtime_checkable
class DiffArtifactStore(Protocol):
    """Whole-object storage boundary for reconciliation diff artifacts.

    Structurally identical to the state store's ``SnapshotObjectStore``:
    ``replace`` has whole-object semantics and ``read`` raises ``KeyError``
    or ``OSError`` when the object is absent, so the same lake storage
    adapters plug in directly.
    """

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Atomically replace the object at a repository-relative path."""
        ...

    def read(self, relative_path: str) -> bytes:
        """Return the full payload of one object."""
        ...


def _safe_token(value: str) -> str:
    if not value or any(character not in _ID_CHARACTERS for character in value):
        raise ValueError(
            f"run identifier {value!r} must contain only letters, digits, "
            "'_', '-', and '.'"
        )
    if value.startswith("."):
        raise ValueError(f"run identifier {value!r} is not a safe path token")
    return value


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _position_map(
    snapshot: PortfolioSnapshot,
) -> dict[str, Decimal]:
    open_positions: dict[str, Decimal] = {}
    for position in snapshot.positions:
        if position.quantity == _ZERO:
            continue
        open_positions[str(position.symbol)] = position.quantity
    return open_positions


def _average_prices(snapshot: PortfolioSnapshot) -> dict[str, Decimal | None]:
    prices: dict[str, Decimal | None] = {}
    for position in snapshot.positions:
        if position.quantity == _ZERO:
            continue
        prices[str(position.symbol)] = position.average_cost
    return prices


def _classify_positions(
    state: BrokerStateSnapshot,
    snapshot: PortfolioSnapshot,
    tolerances: ReconciliationTolerances,
) -> tuple[
    tuple[PositionDelta, ...],
    tuple[PositionDelta, ...],
    tuple[PositionDelta, ...],
]:
    broker_quantities = {
        record.symbol: record.quantity
        for record in state.positions
        if record.quantity != _ZERO
    }
    broker_prices = {record.symbol: record.average_price for record in state.positions}
    virtual_quantities = _position_map(snapshot)
    virtual_prices = _average_prices(snapshot)

    missing = tuple(
        PositionDelta(
            symbol=symbol,
            broker_quantity=broker_quantities[symbol],
            virtual_quantity=None,
            quantity_delta=None,
            broker_average_price=broker_prices.get(symbol),
            virtual_average_price=None,
        )
        for symbol in sorted(set(broker_quantities) - set(virtual_quantities))
    )
    extra = tuple(
        PositionDelta(
            symbol=symbol,
            broker_quantity=None,
            virtual_quantity=virtual_quantities[symbol],
            quantity_delta=None,
            broker_average_price=None,
            virtual_average_price=virtual_prices.get(symbol),
        )
        for symbol in sorted(set(virtual_quantities) - set(broker_quantities))
    )
    mismatched: list[PositionDelta] = []
    for symbol in sorted(set(broker_quantities) & set(virtual_quantities)):
        broker_quantity = broker_quantities[symbol]
        virtual_quantity = virtual_quantities[symbol]
        quantity_delta = broker_quantity - virtual_quantity
        broker_price = broker_prices.get(symbol)
        virtual_price = virtual_prices.get(symbol)
        fields: list[str] = []
        if not tolerances.within("position_quantity", quantity_delta):
            fields.append("quantity")
        price_delta: Decimal | None = None
        if broker_price is not None and virtual_price is not None:
            price_delta = broker_price - virtual_price
            if not tolerances.within("average_price", price_delta):
                fields.append("average_price")
        if fields:
            mismatched.append(
                PositionDelta(
                    symbol=symbol,
                    broker_quantity=broker_quantity,
                    virtual_quantity=virtual_quantity,
                    quantity_delta=quantity_delta,
                    broker_average_price=broker_price,
                    virtual_average_price=virtual_price,
                    mismatched_fields=tuple(fields),
                )
            )
    return missing, extra, tuple(mismatched)


def _classify_orders(
    expected_orders: tuple[ExpectedOrderRecord, ...],
    state: BrokerStateSnapshot,
    tolerances: ReconciliationTolerances,
) -> tuple[
    tuple[OrderDelta, ...],
    tuple[OrderDelta, ...],
    tuple[OrderDelta, ...],
]:
    expected = {order.order_id: order for order in expected_orders}
    reported = {order.order_id: order for order in state.orders}

    missing: list[OrderDelta] = []
    drifted: list[OrderDelta] = []
    for order_id in sorted(expected):
        expectation = expected[order_id]
        broker_order = reported.get(order_id)
        if broker_order is None:
            missing.append(
                OrderDelta(
                    order_id=order_id,
                    expected_status=expectation.status.value,
                    broker_status=None,
                    expected_filled_quantity=expectation.filled_quantity,
                    broker_filled_quantity=None,
                    filled_quantity_delta=None,
                )
            )
            continue
        if broker_order.status.value not in _OPEN_ORDER_STATUSES:
            drifted.append(
                OrderDelta(
                    order_id=order_id,
                    expected_status=expectation.status.value,
                    broker_status=broker_order.status.value,
                    expected_filled_quantity=expectation.filled_quantity,
                    broker_filled_quantity=broker_order.filled_quantity,
                    filled_quantity_delta=(
                        broker_order.filled_quantity - expectation.filled_quantity
                    ),
                    drifted_fields=("status",),
                )
            )
            continue
        fields = []
        if broker_order.status is not expectation.status:
            fields.append("status")
        if broker_order.symbol != expectation.symbol:
            fields.append("symbol")
        if broker_order.side is not expectation.side:
            fields.append("side")
        if not tolerances.within(
            "order_quantity", broker_order.quantity - expectation.quantity
        ):
            fields.append("quantity")
        filled_delta = broker_order.filled_quantity - expectation.filled_quantity
        if not tolerances.within("order_filled_quantity", filled_delta):
            fields.append("filled_quantity")
        if fields:
            drifted.append(
                OrderDelta(
                    order_id=order_id,
                    expected_status=expectation.status.value,
                    broker_status=broker_order.status.value,
                    expected_filled_quantity=expectation.filled_quantity,
                    broker_filled_quantity=broker_order.filled_quantity,
                    filled_quantity_delta=filled_delta,
                    drifted_fields=tuple(fields),
                )
            )

    unexpected = tuple(
        OrderDelta(
            order_id=order_id,
            expected_status=None,
            broker_status=reported[order_id].status.value,
            expected_filled_quantity=None,
            broker_filled_quantity=reported[order_id].filled_quantity,
            filled_quantity_delta=None,
        )
        for order_id in sorted(set(reported) - set(expected))
        if reported[order_id].status.value in _OPEN_ORDER_STATUSES
    )
    return tuple(missing), unexpected, tuple(drifted)


def _classify_cash(
    state: BrokerStateSnapshot,
    snapshot: PortfolioSnapshot,
    tolerances: ReconciliationTolerances,
) -> CashDelta:
    delta = state.account.cash - snapshot.cash
    return CashDelta(
        broker_cash=state.account.cash,
        virtual_cash=snapshot.cash,
        delta=delta,
        within_tolerance=tolerances.within("cash", delta),
    )


# --------------------------------------------------------------------------
# Reconciler
# --------------------------------------------------------------------------


def _artifact_key(run_id: str, sequence: int) -> str:
    return f"runs/{run_id}/reconcile/{sequence:020d}.json"


class RuntimeReconciler:
    """Compare broker state against virtual books on explicit triggers.

    The reconciler is a deterministic kernel component: every comparison is
    triggered by an explicit :meth:`reconcile` call — boot or an engine-clocked
    periodic cadence checked through :meth:`due` — never by a wall-clock
    timer.  Each run classifies the broker-versus-virtual differences into a
    :class:`ReconciliationDiff`, invokes the configured
    :class:`ReconciliationPolicy`, records the audited
    :class:`ReconciliationOutcome`, and, when a :class:`DiffArtifactStore` is
    injected, persists the diff artifact with verify-then-commit semantics.
    """

    __slots__ = (
        "_audit",
        "_broker",
        "_last_sequence",
        "_policy",
        "_run_id",
        "_schedule",
        "_store",
        "_tolerances",
    )

    def __init__(
        self,
        *,
        broker: BrokerStatePort,
        run_id: str,
        policy: ReconciliationPolicy | None = None,
        tolerances: ReconciliationTolerances | None = None,
        schedule: ReconcileSchedule | None = None,
        store: DiffArtifactStore | None = None,
    ) -> None:
        if not isinstance(broker, BrokerStatePort):
            raise TypeError("broker must implement BrokerStatePort")
        if policy is not None and not isinstance(policy, ReconciliationPolicy):
            raise TypeError("policy must implement ReconciliationPolicy")
        if store is not None and not isinstance(store, DiffArtifactStore):
            raise TypeError("store must implement DiffArtifactStore")
        if schedule is not None and not isinstance(schedule, ReconcileSchedule):
            raise TypeError("schedule must be a ReconcileSchedule")
        self._broker = broker
        self._run_id = _safe_token(text(run_id, "run_id"))
        self._policy: ReconciliationPolicy = (
            HaltOnDivergencePolicy() if policy is None else policy
        )
        self._tolerances = (
            tolerances if tolerances is not None else ReconciliationTolerances()
        )
        self._schedule = schedule
        self._store = store
        self._last_sequence: int | None = None
        self._audit: list[ReconciliationOutcome] = []

    @property
    def run_id(self) -> str:
        """Return the deployment run identifier this reconciler is bound to."""
        return self._run_id

    @property
    def audit_log(self) -> tuple[ReconciliationOutcome, ...]:
        """Return every recorded outcome in reconciliation order."""
        return tuple(self._audit)

    @property
    def last_sequence(self) -> int | None:
        """Return the sequence of the most recent reconciliation, if any."""
        return self._last_sequence

    def due(self, market_event_count: int) -> bool:
        """Return whether the periodic schedule is due at this event count."""
        if self._schedule is None:
            return False
        return self._schedule.due(market_event_count)

    def reconcile(
        self,
        *,
        hub: PortfolioHub,
        trigger: ReconcileTrigger,
        sequence: int,
        expected_orders: Sequence[ExpectedOrderRecord] = (),
        observed_at: datetime | None = None,
    ) -> ReconciliationOutcome:
        """Run one reconciliation and return its audited outcome.

        The comparison is exact: positions, open orders, and cash from the
        broker port are classified against the hub's frozen aggregate
        snapshot under the configured tolerances.  ``observed_at`` is an
        optional explicit engine-clocked stamp; the reconciler never reads
        wall time itself.
        """
        if not isinstance(hub, PortfolioHub):
            raise TypeError("hub must be a PortfolioHub")
        if not isinstance(trigger, ReconcileTrigger):
            raise TypeError("trigger must be a ReconcileTrigger")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if self._last_sequence is not None and sequence <= self._last_sequence:
            raise ReconcilerError(
                f"sequence {sequence} does not advance past {self._last_sequence}"
            )
        observed: datetime | None = (
            None if observed_at is None else timestamp(observed_at, "observed_at")
        )
        expectations = tuple(expected_orders)
        expectation_ids = [order.order_id for order in expectations]
        if len(set(expectation_ids)) != len(expectation_ids):
            raise ReconcilerError("expected_orders must not repeat an order identity")
        state = self._fetch_state()
        snapshot = hub.snapshot

        missing, extra, mismatched = _classify_positions(
            state, snapshot, self._tolerances
        )
        orders_missing, orders_unexpected, orders_drifted = _classify_orders(
            expectations, state, self._tolerances
        )
        diff = ReconciliationDiff(
            positions_missing=missing,
            positions_extra=extra,
            positions_mismatched=mismatched,
            orders_missing=orders_missing,
            orders_unexpected=orders_unexpected,
            orders_drifted=orders_drifted,
            cash=_classify_cash(state, snapshot, self._tolerances),
        )
        decision = self._policy.decide(diff)
        outcome = ReconciliationOutcome(
            run_id=self._run_id,
            trigger=trigger,
            sequence=sequence,
            observed_at=observed,
            diff=diff,
            decision=decision,
            decision_source=type(self._policy).__name__,
        )
        self._audit.append(outcome)
        self._last_sequence = sequence
        if self._store is not None:
            self._persist(outcome)
        return outcome

    def _fetch_state(self) -> BrokerStateSnapshot:
        state = self._broker.fetch_state()
        if not isinstance(state, BrokerStateSnapshot):
            raise ReconciliationBrokerError(
                "broker port must return a BrokerStateSnapshot"
            )
        return state

    def _persist(self, outcome: ReconciliationOutcome) -> None:
        if self._store is None:
            return
        key = _artifact_key(self._run_id, outcome.sequence)
        payload = outcome.artifact_bytes
        try:
            self._store.replace(key, payload)
            stored = self._store.read(key)
        except OSError as error:
            raise ReconciliationStoreError(
                f"diff artifact at {key!r} could not be persisted: {error}"
            ) from error
        if stored != payload:
            raise ReconciliationStoreError(
                f"diff artifact at {key!r} did not persist verbatim"
            )


__all__ = [
    "FORMAT_VERSION",
    "BrokerAccountRecord",
    "BrokerOrderRecord",
    "BrokerPositionRecord",
    "BrokerStatePort",
    "BrokerStateSnapshot",
    "CashDelta",
    "DefaultHaltPolicy",
    "DiffArtifactStore",
    "ExpectedOrderRecord",
    "HaltOnDivergencePolicy",
    "OrderDelta",
    "PositionDelta",
    "ReconcileDecision",
    "ReconcileSchedule",
    "ReconcileTrigger",
    "ReconcilerError",
    "ReconciliationBrokerError",
    "ReconciliationDiff",
    "ReconciliationOutcome",
    "ReconciliationPolicy",
    "ReconciliationStoreError",
    "ReconciliationTolerances",
    "RuntimeReconciler",
    "StaticPolicy",
]
