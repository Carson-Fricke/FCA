"""Boot recovery policies for the paper runtime: the recovery taxonomy.

A killed or operator-stopped runtime boots again through exactly one explicit
:class:`BootPolicy` object executed by :class:`RuntimeRecovery`:

- :class:`ResumeReconcilePolicy` loads the latest snapshot through
  :class:`~harness.runtime.state_store.RuntimeStateStore`, restores the
  virtual books, reconciles the broker against them through
  :class:`~harness.runtime.reconciler.RuntimeReconciler` — the one shared
  reconciliation implementation — and continues dispatch only when the
  reconciliation decision proceeds; an ``abort`` decision halts.
- :class:`FlattenHaltPolicy` cancels every open order in deterministic
  order, flattens every open position to cash through hub accounting at
  explicit marks, and halts.
- :class:`HaltOnlyPolicy` preserves the restored state untouched and halts
  dispatch.

The recovery taxonomy is fail-closed: every fault — an unreadable or torn
snapshot, a broker port that cannot serve state mid-reconcile, a cancel
command that fails, a missing flatten mark, even a failing halt mechanism —
deterministically ends in an audited :attr:`RecoveryAction.HALT` decision
recorded on the returned :class:`RecoveryReport`, never in a silent proceed.
The report carries the complete phase trail, the reconciliation outcome with
its canonical diff artifact, the cancel-all and flatten outcomes, and a
domain-separated SHA-256 report identity, so every boot decision is
auditable after the fact.

Cancel-all sequencing is deterministic (deduplicated lexicographic order)
and idempotent: identifiers already canceled by an earlier pass are skipped,
so a retried recovery issues no duplicate cancel commands.  Flattening is
idempotent by position: an already-flat book is left untouched, and flatten
fill identifiers are derived from the book and symbol they close, so a
retried flatten after a partial pass never collides.

The synchronous kernel surface here reads no wall time, draws no randomness,
and performs no network I/O.  Every time stamp and sequence number enters
through explicit arguments, and side effects cross the injected
:class:`HaltPort` and :class:`OrderCancelPort` boundaries only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from random import Random
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types import Fill, Side
from harness.core.types.common import DecimalInput, exact_decimal, text, timestamp
from harness.portfolio import PortfolioHub
from harness.risk.kill_switch import KillSwitch
from harness.runtime.reconciler import (
    ReconcileDecision,
    ReconcileTrigger,
    ReconciliationOutcome,
    RuntimeReconciler,
)
from harness.runtime.state_store import RuntimeSnapshot, RuntimeStateStore

FORMAT_VERSION: Final = 1

_REPORT_DOMAIN: Final = b"fca:harness-runtime-recovery-report:v1\0"
_ZERO: Final = Decimal("0")
_ID_CHARACTERS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class BootPolicyKind(StrEnum):
    """The three explicit boot recovery policies."""

    RESUME_RECONCILE = "resume_reconcile"
    FLATTEN_HALT = "flatten_halt"
    HALT_ONLY = "halt_only"


class RecoveryAction(StrEnum):
    """The final dispatch decision of one recovery pass."""

    RESUME = "resume"
    HALT = "halt"


class RecoveryPhase(StrEnum):
    """The audited phases a recovery pass can execute."""

    SNAPSHOT_LOAD = "snapshot_load"
    STATE_RESTORE = "state_restore"
    RECONCILE = "reconcile"
    CANCEL_ALL = "cancel_all"
    FLATTEN = "flatten"
    STATE_PRESERVED = "state_preserved"
    HALT = "halt"
    RESUME = "resume"


class RecoveryError(RuntimeError):
    """Raised when a recovery composition or invocation is invalid."""


# --------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------


@runtime_checkable
class HaltPort(Protocol):
    """Injected dispatch-halt boundary, such as a kill switch or shell stop."""

    def halt(self, *, reason: str) -> None:
        """Halt dispatch with an explicit, audited reason."""
        ...


@runtime_checkable
class OrderCancelPort(Protocol):
    """Injected order-command boundary used by cancel-all sequencing."""

    def cancel_order(self, order_id: str) -> None:
        """Request the cancel of one open order."""
        ...


@dataclass(frozen=True, slots=True)
class KillSwitchHaltPort:
    """Halt port adapter that trips an audited :class:`KillSwitch`."""

    switch: KillSwitch
    actor: str = "runtime-recovery"

    def __post_init__(self) -> None:
        if not isinstance(self.switch, KillSwitch):
            raise TypeError("switch must be a KillSwitch")
        object.__setattr__(self, "actor", text(self.actor, "actor"))

    def halt(self, *, reason: str) -> None:
        """Trip the wrapped switch with the recovery reason as provenance."""
        self.switch.trip(reason=text(reason, "reason"), actor=self.actor)


# --------------------------------------------------------------------------
# Policy objects
# --------------------------------------------------------------------------


class BootPolicy(Protocol):
    """Explicit boot recovery policy executed by :class:`RuntimeRecovery`."""

    @property
    def kind(self) -> BootPolicyKind:
        """Return the policy identity this object selects."""
        ...


@dataclass(frozen=True, slots=True)
class ResumeReconcilePolicy:
    """Load the latest snapshot, restore, reconcile, and continue iff the
    reconciliation decision proceeds.

    The reconciliation itself is delegated to the injected
    :class:`~harness.runtime.reconciler.RuntimeReconciler` and its configured
    policy, so broker-versus-book divergence follows exactly one audited
    implementation: a ``proceed`` or ``log`` decision resumes dispatch, and
    an ``abort`` decision — the default
    :class:`~harness.runtime.reconciler.HaltOnDivergencePolicy` stance —
    halts with the complete diff artifact preserved on the report.
    """

    @property
    def kind(self) -> BootPolicyKind:
        """Return :attr:`BootPolicyKind.RESUME_RECONCILE`."""
        return BootPolicyKind.RESUME_RECONCILE


@dataclass(frozen=True, slots=True)
class FlattenHaltPolicy:
    """Cancel every open order, flatten positions to cash, and halt.

    Cancel-all runs first and must complete: a failed cancel is fail-closed
    and the pass halts without touching positions, because orders that may
    still fill must never race the liquidation.  Positions are flattened
    through hub accounting at explicit marks so every book ends in cash.
    """

    @property
    def kind(self) -> BootPolicyKind:
        """Return :attr:`BootPolicyKind.FLATTEN_HALT`."""
        return BootPolicyKind.FLATTEN_HALT


@dataclass(frozen=True, slots=True)
class HaltOnlyPolicy:
    """Preserve the restored state untouched and halt dispatch.

    No cancels, no flattening, and no reconciliation run: the pass records
    the preserved state identity, invokes the halt boundary once, and stops.
    """

    @property
    def kind(self) -> BootPolicyKind:
        """Return :attr:`BootPolicyKind.HALT_ONLY`."""
        return BootPolicyKind.HALT_ONLY


_RESUME_DECISIONS: Final[Mapping[ReconcileDecision, RecoveryAction]] = MappingProxyType(
    {
        ReconcileDecision.PROCEED: RecoveryAction.RESUME,
        ReconcileDecision.LOG: RecoveryAction.RESUME,
        ReconcileDecision.ABORT: RecoveryAction.HALT,
    }
)


# --------------------------------------------------------------------------
# Outcome records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryStep:
    """One audited phase execution or fault within a recovery pass."""

    phase: RecoveryPhase
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.phase, RecoveryPhase):
            raise TypeError("phase must be a RecoveryPhase")
        object.__setattr__(self, "detail", text(self.detail, "detail"))

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready step record."""
        return {"phase": self.phase.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """One deterministic, idempotent cancel-all execution."""

    requested: tuple[str, ...]
    canceled: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: str | None
    complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested", tuple(self.requested))
        object.__setattr__(self, "canceled", tuple(self.canceled))
        object.__setattr__(self, "skipped", tuple(self.skipped))
        if self.failed is not None:
            object.__setattr__(self, "failed", text(self.failed, "failed"))
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready cancel record."""
        return {
            "requested": list(self.requested),
            "canceled": list(self.canceled),
            "skipped": list(self.skipped),
            "failed": self.failed,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class FlattenOutcome:
    """One flatten-to-cash execution through hub accounting."""

    books: tuple[str, ...]
    fills: tuple[Fill, ...]
    liquidated_value: Decimal
    fees: Decimal
    flat: bool

    def __post_init__(self) -> None:
        books = tuple(self.books)
        for book_id in books:
            text(book_id, "book_id")
        object.__setattr__(self, "books", books)
        fills = tuple(self.fills)
        for fill in fills:
            if not isinstance(fill, Fill):
                raise TypeError("fills must contain Fill values")
        object.__setattr__(self, "fills", fills)
        object.__setattr__(
            self,
            "liquidated_value",
            exact_decimal(self.liquidated_value, "liquidated_value"),
        )
        object.__setattr__(self, "fees", exact_decimal(self.fees, "fees"))
        if not isinstance(self.flat, bool):
            raise TypeError("flat must be a boolean")

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready flatten record."""
        return {
            "books": list(self.books),
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "symbol": str(fill.symbol),
                    "side": fill.side.value,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "timestamp": fill.timestamp,
                }
                for fill in self.fills
            ],
            "liquidated_value": self.liquidated_value,
            "fees": self.fees,
            "flat": self.flat,
        }


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """The audited result of one boot recovery pass."""

    run_id: str
    policy: BootPolicyKind
    action: RecoveryAction
    steps: tuple[RecoveryStep, ...]
    event_time: datetime | None = None
    snapshot_id: str | None = None
    failure: str | None = None
    reconcile: ReconciliationOutcome | None = None
    cancels: CancelOutcome | None = None
    flatten: FlattenOutcome | None = None
    hub: PortfolioHub | None = field(default=None, compare=False, repr=False)
    randoms: Mapping[str, Random] | None = field(
        default=None, compare=False, repr=False
    )
    strategy_state: Mapping[str, Mapping[str, object]] | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        text(self.run_id, "run_id")
        if not isinstance(self.policy, BootPolicyKind):
            raise TypeError("policy must be a BootPolicyKind")
        if not isinstance(self.action, RecoveryAction):
            raise TypeError("action must be a RecoveryAction")
        steps = tuple(self.steps)
        for step in steps:
            if not isinstance(step, RecoveryStep):
                raise TypeError("steps must contain RecoveryStep values")
        object.__setattr__(self, "steps", steps)
        observed = (
            None
            if self.event_time is None
            else timestamp(self.event_time, "event_time")
        )
        object.__setattr__(self, "event_time", observed)
        if self.snapshot_id is not None:
            _require_digest(self.snapshot_id, "snapshot_id")
        if self.failure is not None:
            object.__setattr__(self, "failure", text(self.failure, "failure"))
        if self.reconcile is not None and not isinstance(
            self.reconcile, ReconciliationOutcome
        ):
            raise TypeError("reconcile must be a ReconciliationOutcome")
        if self.cancels is not None and not isinstance(self.cancels, CancelOutcome):
            raise TypeError("cancels must be a CancelOutcome")
        if self.flatten is not None and not isinstance(self.flatten, FlattenOutcome):
            raise TypeError("flatten must be a FlattenOutcome")
        if self.hub is not None and not isinstance(self.hub, PortfolioHub):
            raise TypeError("hub must be a PortfolioHub")
        object.__setattr__(self, "randoms", MappingProxyType(dict(self.randoms or {})))
        object.__setattr__(
            self,
            "strategy_state",
            MappingProxyType(dict(self.strategy_state or {})),
        )

    @property
    def report_id(self) -> str:
        """Return the domain-separated SHA-256 identity of this report."""
        return hashlib.sha256(
            _REPORT_DOMAIN + canonical_configuration_bytes(self.to_json())
        ).hexdigest()

    def to_json(self) -> dict[str, object]:
        """Return the canonical JSON-ready audit record."""
        return {
            "format_version": FORMAT_VERSION,
            "run_id": self.run_id,
            "policy": self.policy.value,
            "action": self.action.value,
            "steps": [step.to_json() for step in self.steps],
            "event_time": self.event_time,
            "snapshot_id": self.snapshot_id,
            "failure": self.failure,
            "reconcile": None if self.reconcile is None else self.reconcile.to_json(),
            "cancels": None if self.cancels is None else self.cancels.to_json(),
            "flatten": None if self.flatten is None else self.flatten.to_json(),
        }


# --------------------------------------------------------------------------
# Kernel primitives: cancel-all sequencing and flatten-to-cash
# --------------------------------------------------------------------------


def cancel_all(
    port: OrderCancelPort,
    order_ids: Sequence[str],
    *,
    already_canceled: Collection[str] = (),
) -> CancelOutcome:
    """Cancel open orders in deterministic order; idempotent.

    Identifiers are deduplicated and commanded in lexicographic order, so
    every pass over the same open-order set commands the same sequence.  A
    cancel command that raises stops the sequence at that identifier — the
    remaining orders are never commanded — and is reported through
    :attr:`CancelOutcome.failed` with :attr:`CancelOutcome.complete` false.
    Identifiers in ``already_canceled`` are skipped without commanding the
    port, so a retried pass never issues a duplicate cancel.
    """
    identifiers = {text(order_id, "order_id") for order_id in order_ids}
    skip = set(already_canceled) & identifiers
    requested = tuple(sorted(identifiers - skip))
    canceled: list[str] = []
    failed: str | None = None
    for order_id in requested:
        try:
            port.cancel_order(order_id)
        except Exception:
            failed = order_id
            break
        canceled.append(order_id)
    return CancelOutcome(
        requested=requested,
        canceled=tuple(canceled),
        skipped=tuple(sorted(skip)),
        failed=failed,
        complete=failed is None,
    )


def flatten_positions(
    hub: PortfolioHub,
    *,
    marks: Mapping[str, DecimalInput],
    run_id: str,
    event_time: datetime | None,
    base_sequence: int,
) -> FlattenOutcome:
    """Flatten every open hub position to cash through hub accounting.

    Books are flattened in lexicographic book order and symbols in canonical
    order; every closing fill is booked at the explicit mark for its symbol,
    so the operation is exact and deterministic.  Flatten fill identifiers
    are ``{run_id}-flatten-{book_id}-{symbol}``, which makes a retried
    flatten after a partial pass collision-free, and the pass itself is
    idempotent: an already-flat book produces no fills.  ``event_time`` and
    ``base_sequence`` are required only when there is something to flatten.
    """
    if not isinstance(hub, PortfolioHub):
        raise TypeError("hub must be a PortfolioHub")
    run = text(run_id, "run_id")
    resolved: dict[str, Decimal] = {}
    for key, price in marks.items():
        symbol = str(canonical_symbol(text(str(key), "mark symbol")))
        resolved[symbol] = exact_decimal(price, f"mark {symbol!r}")
    targets: list[tuple[str, str, Decimal, Side]] = []
    for book in sorted(hub.books, key=lambda item: item.book_id):
        for position in book.snapshot.positions:
            if position.quantity == _ZERO:
                continue
            symbol = str(position.symbol)
            price = resolved.get(symbol)
            if price is None:
                raise RecoveryError(
                    f"no mark supplied for {symbol!r}; cannot flatten book "
                    f"{book.book_id!r}"
                )
            if price <= _ZERO:
                raise RecoveryError(f"mark for {symbol!r} must be positive")
            side = Side.SELL if position.quantity > _ZERO else Side.BUY
            targets.append((book.book_id, symbol, abs(position.quantity), side))
    fills: list[Fill] = []
    if targets:
        if not isinstance(event_time, datetime):
            raise TypeError(
                "event_time must be a timezone-aware datetime when positions "
                "are flattened"
            )
        moment = timestamp(event_time, "event_time")
        if isinstance(base_sequence, bool) or not isinstance(base_sequence, int):
            raise TypeError("base_sequence must be an integer")
        if base_sequence < 0:
            raise ValueError("base_sequence must not be negative")
        for ordinal, (book_id, symbol, quantity, side) in enumerate(targets, start=1):
            fill_id = f"{run}-flatten-{book_id}-{symbol}"
            fill = Fill(
                event_id=fill_id,
                timestamp=moment,
                sequence=base_sequence + ordinal,
                fill_id=fill_id,
                order_id=fill_id,
                symbol=Symbol(symbol),
                side=side,
                quantity=quantity,
                price=resolved[symbol],
                fee=_ZERO,
            )
            hub.apply_fill(book_id, fill)
            fills.append(fill)
    flat = all(
        hub.book(book_id).position(symbol).is_flat for book_id, symbol, _, _ in targets
    )
    liquidated = sum((fill.quantity * fill.price for fill in fills), start=_ZERO)
    fees = sum((fill.fee for fill in fills), start=_ZERO)
    return FlattenOutcome(
        books=tuple(sorted({book_id for book_id, _, _, _ in targets})),
        fills=tuple(fills),
        liquidated_value=liquidated,
        fees=fees,
        flat=flat,
    )


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------


class _RecoveryPass:
    """Mutable step-and-fault accumulator for one recovery pass."""

    __slots__ = ("failure", "steps")

    def __init__(self) -> None:
        self.steps: list[RecoveryStep] = []
        self.failure: str | None = None

    def record(self, phase: RecoveryPhase, detail: str) -> None:
        """Append one audited step."""
        self.steps.append(RecoveryStep(phase, detail))

    def fault(self, phase: RecoveryPhase, message: str) -> None:
        """Record a fault; the first fault of the pass becomes its failure."""
        self.steps.append(RecoveryStep(phase, message))
        if self.failure is None:
            self.failure = message


class RuntimeRecovery:
    """Execute one explicit boot recovery policy against runtime boundaries.

    The coordinator owns no state beyond the accumulated cancel-all memory
    that makes retried passes idempotent.  Every phase is fail-closed: any
    exception raised inside a phase is recorded on the trace and the pass
    deterministically halts, so injected chaos faults can never produce a
    silent continue.
    """

    __slots__ = ("_cancel", "_canceled", "_halt", "_policy", "_reconciler", "_run_id")

    def __init__(
        self,
        *,
        run_id: str,
        policy: BootPolicy,
        halt: HaltPort,
        cancel: OrderCancelPort | None = None,
        reconciler: RuntimeReconciler | None = None,
    ) -> None:
        self._run_id = _safe_token(text(run_id, "run_id"))
        if not isinstance(
            policy, ResumeReconcilePolicy | FlattenHaltPolicy | HaltOnlyPolicy
        ):
            raise RecoveryError(
                "policy must be a ResumeReconcilePolicy, FlattenHaltPolicy, "
                "or HaltOnlyPolicy"
            )
        if not isinstance(halt, HaltPort):
            raise RecoveryError("halt must implement HaltPort")
        if cancel is not None and not isinstance(cancel, OrderCancelPort):
            raise RecoveryError("cancel must implement OrderCancelPort")
        if policy.kind is BootPolicyKind.RESUME_RECONCILE:
            if reconciler is None:
                raise RecoveryError(
                    "the resume-reconcile policy requires a RuntimeReconciler"
                )
            if reconciler.run_id != self._run_id:
                raise RecoveryError(
                    f"reconciler run {reconciler.run_id!r} does not match "
                    f"recovery run {self._run_id!r}"
                )
        elif reconciler is not None:
            raise RecoveryError(
                f"the {policy.kind.value} policy does not accept a reconciler"
            )
        if policy.kind is BootPolicyKind.FLATTEN_HALT and cancel is None:
            raise RecoveryError("the flatten-halt policy requires an OrderCancelPort")
        if policy.kind is not BootPolicyKind.FLATTEN_HALT and cancel is not None:
            raise RecoveryError(
                f"the {policy.kind.value} policy does not accept a cancel port"
            )
        self._policy = policy
        self._halt = halt
        self._cancel = cancel
        self._reconciler = reconciler
        self._canceled: frozenset[str] = frozenset()

    @property
    def run_id(self) -> str:
        """Return the deployment run identifier this coordinator is bound to."""
        return self._run_id

    @property
    def policy(self) -> BootPolicy:
        """Return the configured boot policy object."""
        return self._policy

    def recover(
        self,
        *,
        hub: PortfolioHub | None = None,
        snapshot: RuntimeSnapshot | None = None,
        store: RuntimeStateStore | None = None,
        open_order_ids: Sequence[str] = (),
        marks: Mapping[str, DecimalInput] | None = None,
        event_time: datetime | None = None,
        reconcile_sequence: int = 0,
        base_sequence: int = 0,
    ) -> RecoveryReport:
        """Run one recovery pass and return its audited report.

        Which phases execute is decided solely by the configured policy:
        resume-reconcile loads and restores state and reconciles; flatten-halt
        resolves the books, cancels open orders, flattens to cash, and halts;
        halt-only preserves whatever state is present and halts.
        """
        if snapshot is not None and not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("snapshot must be a RuntimeSnapshot")
        if store is not None and not isinstance(store, RuntimeStateStore):
            raise TypeError("store must be a RuntimeStateStore")
        if hub is not None and not isinstance(hub, PortfolioHub):
            raise TypeError("hub must be a PortfolioHub")
        if store is not None and store.run_id != self._run_id:
            raise RecoveryError(
                f"store run {store.run_id!r} does not match recovery run "
                f"{self._run_id!r}"
            )
        if snapshot is not None and snapshot.run_id != self._run_id:
            raise RecoveryError(
                f"snapshot run {snapshot.run_id!r} does not match recovery run "
                f"{self._run_id!r}"
            )
        if event_time is not None:
            timestamp(event_time, "event_time")
        _nonnegative(reconcile_sequence, "reconcile_sequence")
        _nonnegative(base_sequence, "base_sequence")
        handler = {
            BootPolicyKind.RESUME_RECONCILE: self._resume_reconcile,
            BootPolicyKind.FLATTEN_HALT: self._flatten_halt,
            BootPolicyKind.HALT_ONLY: self._halt_only,
        }[self._policy.kind]
        return handler(
            hub=hub,
            snapshot=snapshot,
            store=store,
            open_order_ids=open_order_ids,
            marks=marks,
            event_time=event_time,
            reconcile_sequence=reconcile_sequence,
            base_sequence=base_sequence,
        )

    # -- resume-reconcile ------------------------------------------------------

    def _resume_reconcile(
        self,
        *,
        hub: PortfolioHub | None,
        snapshot: RuntimeSnapshot | None,
        store: RuntimeStateStore | None,
        open_order_ids: Sequence[str],
        marks: Mapping[str, DecimalInput] | None,
        event_time: datetime | None,
        reconcile_sequence: int,
        base_sequence: int,
    ) -> RecoveryReport:
        del hub, open_order_ids, marks, base_sequence
        if store is None:
            raise RecoveryError("the resume-reconcile policy requires a state store")
        trace = _RecoveryPass()
        if snapshot is None:
            try:
                snapshot = store.load()
            except Exception as error:
                trace.fault(
                    RecoveryPhase.SNAPSHOT_LOAD, f"snapshot load failed: {error}"
                )
                return self._halted(
                    trace, reason="recovery:snapshot_load_failed", event_time=event_time
                )
            trace.record(
                RecoveryPhase.SNAPSHOT_LOAD, f"loaded snapshot {snapshot.snapshot_id}"
            )
        else:
            trace.record(
                RecoveryPhase.SNAPSHOT_LOAD, f"snapshot {snapshot.snapshot_id} supplied"
            )
        try:
            restored = store.restore_hub(snapshot)
            randoms = store.restore_randoms(snapshot)
            strategy_state = store.restore_strategy_state(snapshot)
        except Exception as error:
            trace.fault(RecoveryPhase.STATE_RESTORE, f"state restore failed: {error}")
            return self._halted(
                trace,
                reason="recovery:state_restore_failed",
                event_time=event_time,
                snapshot_id=snapshot.snapshot_id,
            )
        trace.record(
            RecoveryPhase.STATE_RESTORE, f"restored {len(snapshot.books)} books"
        )
        assert self._reconciler is not None
        try:
            outcome = self._reconciler.reconcile(
                hub=restored,
                trigger=ReconcileTrigger.BOOT,
                sequence=reconcile_sequence,
                observed_at=event_time,
            )
        except Exception as error:
            trace.fault(RecoveryPhase.RECONCILE, f"boot reconciliation failed: {error}")
            return self._halted(
                trace,
                reason="recovery:reconcile_failed",
                event_time=event_time,
                snapshot_id=snapshot.snapshot_id,
                hub=restored,
            )
        trace.record(
            RecoveryPhase.RECONCILE,
            f"decision={outcome.decision.value} diff_id={outcome.diff_id}",
        )
        if _RESUME_DECISIONS[outcome.decision] is RecoveryAction.HALT:
            return self._halted(
                trace,
                reason=f"recovery:resume_reconcile:{outcome.decision.value}",
                event_time=event_time,
                snapshot_id=snapshot.snapshot_id,
                reconcile=outcome,
                hub=restored,
            )
        trace.record(
            RecoveryPhase.RESUME, "continuing dispatch from the restored state"
        )
        return RecoveryReport(
            run_id=self._run_id,
            policy=self._policy.kind,
            action=RecoveryAction.RESUME,
            steps=tuple(trace.steps),
            event_time=event_time,
            snapshot_id=snapshot.snapshot_id,
            reconcile=outcome,
            hub=restored,
            randoms=randoms,
            strategy_state=strategy_state,
        )

    # -- flatten-halt ----------------------------------------------------------

    def _flatten_halt(
        self,
        *,
        hub: PortfolioHub | None,
        snapshot: RuntimeSnapshot | None,
        store: RuntimeStateStore | None,
        open_order_ids: Sequence[str],
        marks: Mapping[str, DecimalInput] | None,
        event_time: datetime | None,
        reconcile_sequence: int,
        base_sequence: int,
    ) -> RecoveryReport:
        del reconcile_sequence
        assert self._cancel is not None
        trace = _RecoveryPass()
        if hub is None:
            if store is None:
                raise RecoveryError(
                    "the flatten-halt policy requires a hub or a state store"
                )
            if snapshot is None:
                try:
                    snapshot = store.load()
                except Exception as error:
                    trace.fault(
                        RecoveryPhase.SNAPSHOT_LOAD, f"snapshot load failed: {error}"
                    )
                    return self._halted(
                        trace,
                        reason="recovery:snapshot_load_failed",
                        event_time=event_time,
                    )
                trace.record(
                    RecoveryPhase.SNAPSHOT_LOAD,
                    f"loaded snapshot {snapshot.snapshot_id}",
                )
            else:
                trace.record(
                    RecoveryPhase.SNAPSHOT_LOAD,
                    f"snapshot {snapshot.snapshot_id} supplied",
                )
            try:
                hub = store.restore_hub(snapshot)
            except Exception as error:
                trace.fault(
                    RecoveryPhase.STATE_RESTORE, f"state restore failed: {error}"
                )
                return self._halted(
                    trace,
                    reason="recovery:state_restore_failed",
                    event_time=event_time,
                    snapshot_id=snapshot.snapshot_id,
                )
            trace.record(
                RecoveryPhase.STATE_RESTORE, f"restored {len(snapshot.books)} books"
            )
        resolved: dict[str, DecimalInput] = {}
        if snapshot is not None:
            resolved.update(snapshot.prices)
        if marks is not None:
            resolved.update(marks)
        try:
            cancels = cancel_all(
                self._cancel, open_order_ids, already_canceled=self._canceled
            )
        except Exception as error:
            trace.fault(RecoveryPhase.CANCEL_ALL, f"cancel-all failed: {error}")
            return self._halted(
                trace,
                reason="recovery:cancel_all_failed",
                event_time=event_time,
                snapshot_id=_snapshot_id(snapshot),
                hub=hub,
            )
        self._canceled = frozenset(self._canceled | set(cancels.canceled))
        trace.record(
            RecoveryPhase.CANCEL_ALL,
            f"canceled {len(cancels.canceled)} of {len(cancels.requested)} "
            f"requested orders, {len(cancels.skipped)} skipped",
        )
        if not cancels.complete:
            trace.fault(
                RecoveryPhase.CANCEL_ALL, f"cancel-all incomplete at {cancels.failed!r}"
            )
            return self._halted(
                trace,
                reason="recovery:cancel_all_incomplete",
                event_time=event_time,
                snapshot_id=_snapshot_id(snapshot),
                cancels=cancels,
                hub=hub,
            )
        try:
            flattened = flatten_positions(
                hub,
                marks=resolved,
                run_id=self._run_id,
                event_time=event_time,
                base_sequence=base_sequence,
            )
        except Exception as error:
            trace.fault(RecoveryPhase.FLATTEN, f"flatten failed: {error}")
            return self._halted(
                trace,
                reason="recovery:flatten_failed",
                event_time=event_time,
                snapshot_id=_snapshot_id(snapshot),
                cancels=cancels,
                hub=hub,
            )
        trace.record(
            RecoveryPhase.FLATTEN,
            f"applied {len(flattened.fills)} flatten fills across "
            f"{len(flattened.books)} books",
        )
        return self._halted(
            trace,
            reason="recovery:flatten_halt",
            event_time=event_time,
            snapshot_id=_snapshot_id(snapshot),
            cancels=cancels,
            flatten=flattened,
            hub=hub,
        )

    # -- halt-only ---------------------------------------------------------------

    def _halt_only(
        self,
        *,
        hub: PortfolioHub | None,
        snapshot: RuntimeSnapshot | None,
        store: RuntimeStateStore | None,
        open_order_ids: Sequence[str],
        marks: Mapping[str, DecimalInput] | None,
        event_time: datetime | None,
        reconcile_sequence: int,
        base_sequence: int,
    ) -> RecoveryReport:
        del open_order_ids, marks, reconcile_sequence, base_sequence
        trace = _RecoveryPass()
        snapshot_id: str | None = None
        if snapshot is not None:
            snapshot_id = snapshot.snapshot_id
            trace.record(
                RecoveryPhase.SNAPSHOT_LOAD, f"snapshot {snapshot.snapshot_id} supplied"
            )
        elif store is not None:
            try:
                pointer = store.latest()
            except Exception as error:
                trace.fault(
                    RecoveryPhase.SNAPSHOT_LOAD,
                    f"snapshot pointer unavailable: {error}",
                )
            else:
                snapshot_id = pointer.snapshot_id
                trace.record(
                    RecoveryPhase.SNAPSHOT_LOAD,
                    f"latest snapshot pointer {pointer.snapshot_id}",
                )
        if hub is not None:
            trace.record(
                RecoveryPhase.STATE_PRESERVED,
                f"preserved {len(hub.books)} books untouched",
            )
        return self._halted(
            trace,
            reason="recovery:halt_only",
            event_time=event_time,
            snapshot_id=snapshot_id,
            hub=hub,
        )

    # -- halt --------------------------------------------------------------------

    def _halted(
        self,
        trace: _RecoveryPass,
        *,
        reason: str,
        event_time: datetime | None,
        snapshot_id: str | None = None,
        reconcile: ReconciliationOutcome | None = None,
        cancels: CancelOutcome | None = None,
        flatten: FlattenOutcome | None = None,
        hub: PortfolioHub | None = None,
        randoms: Mapping[str, Random] | None = None,
        strategy_state: Mapping[str, Mapping[str, object]] | None = None,
    ) -> RecoveryReport:
        try:
            self._halt.halt(reason=reason)
        except Exception as error:
            trace.fault(RecoveryPhase.HALT, f"halt mechanism failed: {error}")
        else:
            trace.record(RecoveryPhase.HALT, f"halt port invoked ({reason})")
        return RecoveryReport(
            run_id=self._run_id,
            policy=self._policy.kind,
            action=RecoveryAction.HALT,
            steps=tuple(trace.steps),
            event_time=event_time,
            snapshot_id=snapshot_id,
            failure=trace.failure,
            reconcile=reconcile,
            cancels=cancels,
            flatten=flatten,
            hub=hub,
            randoms=randoms,
            strategy_state=strategy_state,
        )


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _snapshot_id(snapshot: RuntimeSnapshot | None) -> str | None:
    return None if snapshot is None else snapshot.snapshot_id


def _nonnegative(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _require_digest(value: str, name: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _safe_token(value: str) -> str:
    if not value or any(character not in _ID_CHARACTERS for character in value):
        raise ValueError(
            f"run identifier {value!r} must contain only letters, digits, "
            "'_', '-', and '.'"
        )
    if value.startswith("."):
        raise ValueError(f"run identifier {value!r} is not a safe path token")
    return value


__all__ = [
    "FORMAT_VERSION",
    "BootPolicy",
    "BootPolicyKind",
    "CancelOutcome",
    "FlattenHaltPolicy",
    "FlattenOutcome",
    "HaltOnlyPolicy",
    "HaltPort",
    "KillSwitchHaltPort",
    "OrderCancelPort",
    "RecoveryAction",
    "RecoveryError",
    "RecoveryPhase",
    "RecoveryReport",
    "RecoveryStep",
    "ResumeReconcilePolicy",
    "RuntimeRecovery",
    "cancel_all",
    "flatten_positions",
]
