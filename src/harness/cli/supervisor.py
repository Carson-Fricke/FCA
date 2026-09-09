"""Trader supervision kernel: one audited session from boot to clean exit.

The supervisor owns the deployment lifecycle the platform cannot: it gates
every boot on the operator kill switch, restores interrupted sessions through
the recovery taxonomy, drives one paper or shadow shell over a live/replay
market-data boundary, and tears the session down with a durable snapshot.

Supervision is a composition, not a second engine: the kernel, venue, risk
pipeline, and virtual books are exactly the :class:`~harness.runtime.paper.
PaperShell` and :class:`~harness.runtime.shadow.ShadowShell` capabilities.
The supervisor adds only the deployment behavior around them:

- Deterministic core: every wall-clock value enters through the injected
  ``clock`` boundary; every blocking pace and retry backoff goes through the
  injected ``sleep`` boundary.  Given the same clock, configuration, ports,
  and market events, the supervision decisions and their audit records are
  identical.
- Fail-closed boot: the kill-switch gate must observe an explicit
  disengaged state (or no remote source) before any session runs; a poll
  error is a restartable fault, never a silent proceed.
- Bounded restarts: transient faults retry with backoff inside one process
  and then exit :attr:`TraderExitCode.RESTARTABLE_FAULT`; across process
  restarts a durable ledger counts consecutive faults and refuses to boot
  with :attr:`TraderExitCode.CRASH_LOOP` once the restart budget is spent.
- Auditable progress: every phase transition and every heartbeat is written
  to the ``supervision`` document in the runtime state store, so
  ``harness trader status`` reports phase, watermark age, and book summary
  without scraping logs.

The kernel classes in this module read no wall clock, draw no randomness,
and open no sockets.  The Azure Table kill-switch poller and the storage
adapters at the bottom of this module are explicit adapter boundaries that
the command-line composition root wires from the deployment environment.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Protocol, runtime_checkable

from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

from harness.configuration import TraderConfig
from harness.core.clock import Clock
from harness.core.events import EVENT_CLASS_PRIORITY
from harness.core.types import Side
from harness.core.types.common import exact_decimal, text
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.risk import RiskPipeline
from harness.risk.kill_switch import KillSwitch
from harness.runtime import (
    BrokerAccountRecord,
    BrokerPositionRecord,
    BrokerStatePort,
    BrokerStateSnapshot,
    FlattenHaltPolicy,
    HaltOnlyPolicy,
    KillSwitchHaltPort,
    PaperFeed,
    PaperInbox,
    PaperOptions,
    PaperSessionPolicy,
    PaperSessionResult,
    PaperSessionStatus,
    PaperShell,
    ReconcileTrigger,
    RecoveryAction,
    RecoveryReport,
    ResumeReconcilePolicy,
    RuntimeReconciler,
    RuntimeRecovery,
    RuntimeSnapshot,
    RuntimeStateStore,
    ShadowShell,
    SnapshotObjectStore,
    SnapshotTrigger,
    StateSnapshotMissingError,
)
from lake.contracts import ExchangeSession

FORMAT_VERSION: int = 1

_KILL_SWITCH_ACTOR: str = "trader-supervisor"
_RESTARTABLE_PREFIX: str = "restartable fault"
_MARKET_WAIT_PREFIX: str = "market-wait exceeded"
_KILL_SWITCH_PREFIX: str = "kill-switch engaged"
_RECONCILE_PREFIX: str = "reconciliation aborted"
_ZERO: Decimal = Decimal("0")
_LEDGER_HISTORY_LIMIT: int = 8


class SupervisionError(RuntimeError):
    """Base class for trader supervision failures."""


class SupervisionConfigurationError(SupervisionError, ValueError):
    """Raised when the configuration cannot drive a supervised session."""


class FeedBoundaryError(SupervisionError):
    """Raised when the market-data boundary cannot be composed."""


class TraderExitCode(IntEnum):
    """Process exit codes the platform observes on every supervised boot.

    ``SUCCESS``, ``KILL_SWITCH_HALTED``, and the session-close exit are
    deliberate completions; the platform may restart them inside the KEDA
    window, where the gate or the missing session stops the next boot from
    trading.  ``RESTARTABLE_FAULT`` asks the platform for a restart;
    ``CRASH_LOOP`` refuses to boot once the restart budget is spent.
    """

    SUCCESS = 0
    FAILED = 1
    USAGE = 2
    KILL_SWITCH_HALTED = 41
    POLICY_HALTED = 42
    RESTARTABLE_FAULT = 75
    CRASH_LOOP = 86


class SupervisionPhase(StrEnum):
    """Audited supervision phases written to the status document."""

    BOOT = "boot"
    GATE = "gate"
    NO_SESSION = "no_session"
    CRASH_LOOP = "crash_loop"
    RESTORE = "restore"
    CONNECT = "connect"
    RUNNING = "running"
    TEARDOWN = "teardown"
    COMPLETED = "completed"
    AT_CLOSE = "at_close"
    STOPPED = "stopped"
    HALTED = "halted"
    FAULTED = "faulted"


class OutcomeKind(StrEnum):
    """Terminal supervision outcomes recorded on the restart ledger."""

    CLEAN = "clean"
    KILL_SWITCH = "kill_switch"
    POLICY_HALT = "policy_halt"
    FAULT = "fault"


@dataclass(frozen=True, slots=True)
class SupervisorSettings:
    """Validated supervision knobs with deployment-safe defaults.

    Every cadence is a positive duration; the restart budget and stale
    tolerance are positive integers; the initial paper cash is exact and
    positive.  All values are supplied by the deployment boundary through
    environment variables or command-line options.
    """

    boot_policy: str = "resume_reconcile"
    initial_cash: Decimal = Decimal("100000.00")
    heartbeat_cadence: timedelta = timedelta(seconds=60)
    kill_switch_poll: timedelta = timedelta(seconds=10)
    kill_switch_stale: int = 3
    snapshot_cadence: timedelta = timedelta(seconds=60)
    reconcile_cadence: timedelta = timedelta(minutes=15)
    market_wait: timedelta = timedelta(minutes=15)
    idle_pace: timedelta = timedelta(seconds=1)
    connect_attempts: int = 2
    connect_backoff: timedelta = timedelta(seconds=2)
    restart_budget: int = 3

    def __post_init__(self) -> None:
        if self.boot_policy not in ("resume_reconcile", "flatten_halt", "halt_only"):
            raise SupervisionConfigurationError(
                f"unknown boot policy {self.boot_policy!r}"
            )
        object.__setattr__(
            self, "initial_cash", exact_decimal(self.initial_cash, "initial_cash")
        )
        if self.initial_cash <= _ZERO:
            raise SupervisionConfigurationError("initial_cash must be positive")
        for field_name in (
            "heartbeat_cadence",
            "kill_switch_poll",
            "snapshot_cadence",
            "reconcile_cadence",
            "market_wait",
            "idle_pace",
            "connect_backoff",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise SupervisionConfigurationError(
                    f"{field_name} must be a positive duration"
                )
        for field_name in ("kill_switch_stale", "connect_attempts", "restart_budget"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SupervisionConfigurationError(
                    f"{field_name} must be a positive integer"
                )


@dataclass(frozen=True, slots=True)
class KillSwitchObservation:
    """One kill-switch source observation stamped by the supervision clock."""

    engaged: bool
    reason: str | None
    at: datetime


@runtime_checkable
class KillSwitchPoller(Protocol):
    """Injected kill-switch source boundary polled by the supervisor."""

    def poll(self) -> KillSwitchObservation:
        """Return one observation; errors propagate to the supervisor."""
        ...


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    """The terminal result of one supervised boot."""

    run_id: str
    exit_code: TraderExitCode
    phase: SupervisionPhase
    detail: str | None
    recovery: RecoveryReport | None = None
    session: PaperSessionResult | None = None

    @property
    def clean(self) -> bool:
        """Return whether the boot ended as an operator-expected outcome."""
        return self.exit_code in (
            TraderExitCode.SUCCESS,
            TraderExitCode.KILL_SWITCH_HALTED,
        )


# --------------------------------------------------------------------------
# Durable supervision documents
# --------------------------------------------------------------------------


class SessionJournal:
    """Durable phase, heartbeat, and restart-ledger records for one run.

    Both documents are whole objects in the injected snapshot object store
    under ``runs/<run-id>/`` and carry wall-clock provenance supplied by the
    injected clock, matching the shell's boot provenance stamps.
    ``supervision.json`` is the status document read by
    ``harness trader status``; ``restarts.json`` bounds the cross-process
    restart budget.  The composition root passes the same object store the
    run's :class:`~harness.runtime.state_store.RuntimeStateStore` wraps, so
    the supervision documents share the deployment's state container.
    """

    __slots__ = ("_clock", "_run_id", "_store")

    def __init__(
        self, *, store: SnapshotObjectStore, run_id: str, clock: Clock
    ) -> None:
        if not isinstance(store, SnapshotObjectStore):
            raise TypeError("store must implement SnapshotObjectStore")
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        self._store = store
        self._run_id = text(run_id, "run_id")
        self._clock = clock

    @property
    def run_id(self) -> str:
        """Return the deployment run this journal is bound to."""
        return self._run_id

    def record_status(
        self,
        *,
        phase: SupervisionPhase,
        detail: str | None = None,
        shell: PaperShell | ShadowShell | None = None,
        recovery: RecoveryReport | None = None,
        heartbeat: bool = False,
    ) -> None:
        """Replace the status document with the current supervision view."""
        now = self._clock.now()
        result = _session_result(shell) if shell is not None else None
        books = _book_summary(shell) if shell is not None else ()
        document: dict[str, object] = {
            "format_version": FORMAT_VERSION,
            "run_id": self._run_id,
            "phase": phase.value,
            "detail": detail,
            "phase_at": _iso(now),
            "updated_at": _iso(now),
            "last_heartbeat_at": _iso(now) if heartbeat else None,
        }
        if result is not None:
            document["session_status"] = result.status.value
            document["session_reason"] = result.reason
            document["booted_at"] = _iso(result.booted_at)
            document["last_event_time"] = _iso_or_none(result.last_event_time)
            document["market_event_count"] = result.market_event_count
            document["dispatched_count"] = result.dispatched_count
            document["dispatch_hash"] = result.dispatch_hash
        if books:
            document["books"] = list(books)
        if recovery is not None:
            document["recovery"] = {
                "policy": recovery.policy.value,
                "action": recovery.action.value,
                "report_id": recovery.report_id,
                "snapshot_id": recovery.snapshot_id,
                "failure": recovery.failure,
            }
        ledger = self.read_ledger()
        document["consecutive_faults"] = ledger.get("consecutive_faults", 0)
        self._write(self._status_key(), document)

    def read_status(self) -> dict[str, object]:
        """Return the status document, raising ``KeyError`` when absent."""
        return self._read(self._status_key())

    def read_ledger(self) -> dict[str, object]:
        """Return the restart ledger, or a fresh document when absent."""
        try:
            document = self._read(self._ledger_key())
        except (KeyError, OSError):
            return {
                "format_version": FORMAT_VERSION,
                "run_id": self._run_id,
                "consecutive_faults": 0,
                "history": [],
            }
        faults = document.get("consecutive_faults", 0)
        return {
            "format_version": FORMAT_VERSION,
            "run_id": self._run_id,
            "consecutive_faults": faults if isinstance(faults, int) else 0,
            "history": (
                list(history)
                if isinstance(history := document.get("history"), list)
                else []
            ),
        }

    def record_boot_outcome(self, kind: OutcomeKind, detail: str) -> None:
        """Fold one terminal boot outcome into the restart ledger."""
        ledger = self.read_ledger()
        faults_value = ledger["consecutive_faults"]
        faults = faults_value if isinstance(faults_value, int) else 0
        if kind is OutcomeKind.FAULT:
            faults += 1
        elif kind is OutcomeKind.CLEAN:
            faults = 0
        elif kind is OutcomeKind.KILL_SWITCH:
            faults = 0
        raw_history = ledger["history"]
        history = list(raw_history) if isinstance(raw_history, list) else []
        history.append(
            {
                "at": _iso(self._clock.now()),
                "outcome": kind.value,
                "detail": detail,
            }
        )
        self._write(
            self._ledger_key(),
            {
                "format_version": FORMAT_VERSION,
                "run_id": self._run_id,
                "consecutive_faults": faults,
                "history": history[-_LEDGER_HISTORY_LIMIT:],
            },
        )

    def _status_key(self) -> str:
        return f"runs/{self._run_id}/supervision.json"

    def _ledger_key(self) -> str:
        return f"runs/{self._run_id}/restarts.json"

    def _write(self, key: str, document: Mapping[str, object]) -> None:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        self._store.replace(key, payload.encode("utf-8"))

    def _read(self, key: str) -> dict[str, object]:
        raw = self._store.read(key)
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError(f"supervision document {key!r} is not a mapping")
        return document


def _session_result(shell: PaperShell | ShadowShell) -> PaperSessionResult:
    if isinstance(shell, ShadowShell):
        return shell.result().session
    return shell.result()


def _book_summary(shell: PaperShell | ShadowShell) -> tuple[dict[str, object], ...]:
    if isinstance(shell, ShadowShell):
        return ()
    rows: list[dict[str, object]] = []
    for book in shell.hub.books:
        rows.append(
            {
                "book_id": book.book_id,
                "initial_cash": str(book.initial_cash),
                "cash": str(book.cash),
                "equity": (None if book.equity is None else str(book.equity)),
                "positions": [
                    {
                        "symbol": str(position.symbol),
                        "quantity": str(position.quantity),
                    }
                    for position in book.snapshot.positions
                    if position.quantity != _ZERO
                ],
            }
        )
    return tuple(rows)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


# --------------------------------------------------------------------------
# Kernel faults
# --------------------------------------------------------------------------


class RestartableFault(SupervisionError):
    """A transient supervision fault the platform may recover by restart."""


class MarketWaitExceeded(SupervisionError):
    """No market events arrived inside the configured market-wait window."""


# --------------------------------------------------------------------------
# Paper broker-state port
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotBrokerStatePort:
    """Paper-mode broker port derived from the boot snapshot.

    Paper and shadow venues hold no external broker state: the simulated
    venue's positions and cash are exactly the restored books.  This port
    reconstructs the broker view from the snapshot's own fills and cash so
    the boot reconciliation exercises the one shared
    :class:`RuntimeReconciler` against the venue the paper session will
    actually drive — an exact self-consistency check with a full audit
    trail, not a pretend external comparison.
    """

    snapshot: RuntimeSnapshot
    run_id: str

    def fetch_state(self) -> BrokerStateSnapshot:
        """Return the snapshot-derived positions and cash."""
        quantities: dict[str, Decimal] = {}
        for book in self.snapshot.books:
            for fill in book.fills:
                signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
                quantities[str(fill.symbol)] = (
                    quantities.get(str(fill.symbol), _ZERO) + signed
                )
        positions = tuple(
            BrokerPositionRecord(symbol=symbol, quantity=quantity)
            for symbol, quantity in sorted(quantities.items())
            if quantity != _ZERO
        )
        cash = sum((book.cash for book in self.snapshot.books), start=_ZERO)
        return BrokerStateSnapshot(
            account=BrokerAccountRecord(
                account_id=text(self.run_id, "run_id"), cash=cash
            ),
            positions=positions,
            orders=(),
        )


@dataclass(frozen=True, slots=True)
class _RejectingCancelPort:
    """Cancel port for the flatten-halt policy over a paper venue.

    Paper snapshots carry no resting orders, so ``open_order_ids`` is empty
    and no cancel is ever commanded.  Any command is a supervision defect
    and fails closed.
    """

    def cancel_order(self, order_id: str) -> None:
        raise RestartableFault(
            f"paper flatten-halt holds no open orders; cancel of {order_id!r} "
            "is a supervision defect"
        )


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------


class TraderSupervisor:
    """Drive one supervised paper or shadow session to a terminal outcome.

    The supervisor composes the recovery taxonomy, the configured shell,
    the kill-switch gate, and the durable supervision documents.  The
    session pump is the shell's own event loop; supervision work happens
    through the shell's idle-pace boundary between boundary polls and at
    the loop's phase transitions.
    """

    __slots__ = (
        "_clock",
        "_configuration",
        "_connect_attempts",
        "_emit",
        "_feed_factory",
        "_kill_switch",
        "_market_deadline",
        "_mode",
        "_next_heartbeat",
        "_next_kill_switch_poll",
        "_next_periodic_snapshot",
        "_next_periodic_reconcile",
        "_options",
        "_outcome_session",
        "_phase",
        "_poller",
        "_poll_failures",
        "_recovery",
        "_reconciler",
        "_run_id",
        "_session",
        "_settings",
        "_shell",
        "_snapshot",
        "_snapshot_sequence",
        "_state_store",
        "_stop_requested",
        "_journal",
    )

    def __init__(
        self,
        *,
        configuration: TraderConfig,
        settings: SupervisorSettings,
        session: ExchangeSession | None,
        run_id: str,
        state_store: RuntimeStateStore,
        journal: SessionJournal,
        clock: Clock,
        feed_factory: Callable[[], PaperFeed],
        kill_switch: KillSwitch,
        kill_switch_poller: KillSwitchPoller | None = None,
        broker_state: BrokerStatePort | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(configuration, TraderConfig):
            raise SupervisionConfigurationError("configuration must be a TraderConfig")
        if configuration.mode not in ("paper", "shadow"):
            raise SupervisionConfigurationError(
                "supervised sessions support paper and shadow modes; live "
                f"mode requires the go-live gate, got {configuration.mode!r}"
            )
        if not isinstance(settings, SupervisorSettings):
            raise SupervisionConfigurationError("settings must be SupervisorSettings")
        if session is not None and not isinstance(session, ExchangeSession):
            raise SupervisionConfigurationError("session must be an ExchangeSession")
        if not isinstance(state_store, RuntimeStateStore):
            raise SupervisionConfigurationError(
                "state_store must be a RuntimeStateStore"
            )
        if state_store.run_id != run_id:
            raise SupervisionConfigurationError(
                f"state store run {state_store.run_id!r} does not match run {run_id!r}"
            )
        if not isinstance(kill_switch, KillSwitch):
            raise SupervisionConfigurationError("kill_switch must be a KillSwitch")
        self._configuration = configuration
        self._settings = settings
        self._session = session
        self._mode = configuration.mode
        self._run_id = text(run_id, "run_id")
        self._state_store = state_store
        self._journal = journal
        self._clock = clock
        self._feed_factory = feed_factory
        self._kill_switch = kill_switch
        self._poller = kill_switch_poller
        self._reconciler = (
            None
            if broker_state is None
            else RuntimeReconciler(broker=broker_state, run_id=self._run_id)
        )
        self._emit = emit
        self._phase = SupervisionPhase.BOOT
        self._shell: PaperShell | ShadowShell | None = None
        self._options: PaperOptions | None = None
        self._snapshot: RuntimeSnapshot | None = None
        self._recovery: RecoveryReport | None = None
        self._snapshot_sequence = 0
        self._poll_failures = 0
        self._stop_requested = False
        self._market_deadline: datetime | None = None
        self._outcome_session: PaperSessionResult | None = None
        self._next_heartbeat = clock.now()
        self._next_kill_switch_poll = clock.now()
        self._next_periodic_snapshot = clock.now()
        self._next_periodic_reconcile = clock.now()

    @property
    def run_id(self) -> str:
        """Return the supervised run identifier."""
        return self._run_id

    @property
    def phase(self) -> SupervisionPhase:
        """Return the most recent audited supervision phase."""
        return self._phase

    @property
    def kill_switch(self) -> KillSwitch:
        """Return the in-process kill switch driven by the gate and polls."""
        return self._kill_switch

    def request_stop(self, reason: str) -> None:
        """Stop the running session cleanly; safe from a signal handler."""
        self._stop_requested = True
        shell = self._shell
        if shell is not None:
            shell.stop(reason)

    # -- boot ----------------------------------------------------------------

    def run(self) -> SupervisorOutcome:
        """Run one supervised boot to its terminal outcome."""
        try:
            return self._boot()
        except RestartableFault as error:
            return self._fault_outcome(error, TraderExitCode.RESTARTABLE_FAULT)
        except MarketWaitExceeded as error:
            return self._fault_outcome(error, TraderExitCode.RESTARTABLE_FAULT)
        except Exception as error:  # noqa: BLE001 - fail-closed terminal map
            return self._fault_outcome(error, TraderExitCode.FAILED)

    def _boot(self) -> SupervisorOutcome:
        self._transition(SupervisionPhase.BOOT, f"mode={self._mode}")
        if not self._crash_loop_budget_allows_boot():
            return self._outcome(
                TraderExitCode.CRASH_LOOP,
                SupervisionPhase.CRASH_LOOP,
                "restart budget exhausted; refusing to boot",
                OutcomeKind.FAULT,
            )
        if self._kill_switch_gate_blocks_boot():
            return self._outcome(
                TraderExitCode.KILL_SWITCH_HALTED,
                SupervisionPhase.HALTED,
                "kill switch engaged at boot",
                OutcomeKind.KILL_SWITCH,
            )
        if self._session is None:
            return self._outcome(
                TraderExitCode.SUCCESS,
                SupervisionPhase.NO_SESSION,
                "no market session is scheduled for the boot time",
                OutcomeKind.CLEAN,
            )
        self._transition(SupervisionPhase.RESTORE, "boot-policy restore")
        snapshot = self._load_snapshot()
        resumed = False
        if (
            snapshot is not None
            and self._mode == "paper"
            and self._snapshot_matches_session(snapshot)
        ):
            report = self._recover(snapshot)
            if report.action is RecoveryAction.HALT:
                return self._outcome(
                    TraderExitCode.POLICY_HALTED,
                    SupervisionPhase.HALTED,
                    f"boot policy {self._settings.boot_policy} halted: "
                    f"{report.failure or report.policy.value}",
                    OutcomeKind.POLICY_HALT,
                    recovery=report,
                )
            resumed = True
        elif snapshot is not None:
            reason = (
                "shadow sessions boot fresh"
                if self._mode == "shadow"
                else "snapshot session differs from the scheduled session"
            )
            self._transition(
                SupervisionPhase.RESTORE,
                f"snapshot {snapshot.snapshot_id[:12]} superseded: {reason}",
            )
        feed = self._connect()
        self._transition(SupervisionPhase.CONNECT, "market-data boundary connected")
        try:
            shell, options = self._build_shell(snapshot if resumed else None)
            self._shell = shell
            self._options = options
        except Exception:
            feed.close()
            raise
        self._journal_status(SupervisionPhase.RUNNING, "session pump starting")
        result = self._pump(shell, feed)
        self._outcome_session = result
        return self._finish(result)

    # -- gate and budget ------------------------------------------------------

    def _crash_loop_budget_allows_boot(self) -> bool:
        ledger = self._journal.read_ledger()
        faults_value = ledger["consecutive_faults"]
        faults = faults_value if isinstance(faults_value, int) else 0
        if faults < self._settings.restart_budget:
            return True
        self._transition(
            SupervisionPhase.CRASH_LOOP,
            f"{faults} consecutive faulty boots exceed the restart budget",
        )
        return False

    def _kill_switch_gate_blocks_boot(self) -> bool:
        self._transition(SupervisionPhase.GATE, "kill-switch gate")
        if self._poller is None:
            return False
        try:
            observation = self._poller.poll()
        except Exception as error:
            raise RestartableFault(
                f"{_RESTARTABLE_PREFIX}: kill-switch gate poll failed: {error}"
            ) from error
        if not observation.engaged:
            return False
        reason = observation.reason or "operator engaged"
        self._kill_switch.trip(reason=reason, actor=_KILL_SWITCH_ACTOR)
        self._transition(SupervisionPhase.HALTED, f"kill-switch gate: {reason}")
        return True

    # -- restore --------------------------------------------------------------

    def _load_snapshot(self) -> RuntimeSnapshot | None:
        try:
            return self._state_store.load()
        except StateSnapshotMissingError:
            return None
        except Exception as error:
            raise RestartableFault(
                f"{_RESTARTABLE_PREFIX}: snapshot load failed: {error}"
            ) from error

    def _snapshot_matches_session(self, snapshot: RuntimeSnapshot) -> bool:
        options = snapshot.paper_options()
        session = options.session
        if session is None or self._session is None:
            return False
        return (
            session.session_date == self._session.session_date
            and session.open_time == self._session.open_time
            and session.close_time == self._session.close_time
        )

    def _recover(self, snapshot: RuntimeSnapshot) -> RecoveryReport:
        policy = {
            "resume_reconcile": ResumeReconcilePolicy,
            "flatten_halt": FlattenHaltPolicy,
            "halt_only": HaltOnlyPolicy,
        }[self._settings.boot_policy]()
        recovery = RuntimeRecovery(
            run_id=self._run_id,
            policy=policy,
            halt=KillSwitchHaltPort(switch=self._kill_switch, actor=_KILL_SWITCH_ACTOR),
            cancel=_RejectingCancelPort() if policy.kind == "flatten_halt" else None,
            reconciler=(
                RuntimeReconciler(
                    broker=SnapshotBrokerStatePort(
                        snapshot=snapshot, run_id=self._run_id
                    ),
                    run_id=self._run_id,
                )
                if policy.kind == "resume_reconcile"
                else None
            ),
        )
        try:
            if policy.kind == "flatten_halt":
                report = recovery.recover(
                    snapshot=snapshot,
                    store=self._state_store,
                    marks=dict(snapshot.prices),
                    event_time=self._clock.now(),
                )
            else:
                report = recovery.recover(
                    snapshot=snapshot,
                    store=self._state_store,
                    event_time=self._clock.now(),
                )
        except RestartableFault:
            raise
        except Exception as error:
            raise RestartableFault(
                f"{_RESTARTABLE_PREFIX}: recovery pass failed: {error}"
            ) from error
        self._recovery = report
        self._transition(
            SupervisionPhase.RESTORE,
            f"policy={report.policy.value} action={report.action.value} "
            f"report={report.report_id[:12]}",
        )
        return report

    # -- connect --------------------------------------------------------------

    def _connect(self) -> PaperFeed:
        self._transition(SupervisionPhase.CONNECT, "connecting market-data boundary")
        last: Exception | None = None
        for attempt in range(1, self._settings.connect_attempts + 1):
            try:
                return self._feed_factory()
            except Exception as error:
                last = error
                if attempt < self._settings.connect_attempts:
                    self._transition(
                        SupervisionPhase.CONNECT,
                        f"connect attempt {attempt} failed: {error}",
                    )
                    self._sleep(self._settings.connect_backoff.total_seconds())
        raise RestartableFault(
            f"{_RESTARTABLE_PREFIX}: market-data boundary could not be composed "
            f"after {self._settings.connect_attempts} attempts: {last}"
        ) from last

    # -- shell ----------------------------------------------------------------

    def _build_shell(
        self, snapshot: RuntimeSnapshot | None
    ) -> tuple[PaperShell | ShadowShell, PaperOptions]:
        pipeline = self._kill_switch_pipeline()
        if snapshot is not None:
            # The snapshot cannot carry the non-deterministic pacing callback;
            # re-arm it so the resumed boot keeps supervising (heartbeats,
            # periodic snapshots, kill-switch polls, market-wait enforcement).
            pace = self._housekeeping
            options = replace(snapshot.paper_options(), idle_pace=pace)
            shell: PaperShell | ShadowShell = self._state_store.restore_shell(
                snapshot,
                self._configuration,
                wall_clock=self._clock,
                idle_pace=pace,
            )
            return shell, options
        options = self._session_options()
        if self._mode == "shadow":
            return (
                ShadowShell(
                    self._configuration,
                    options=options,
                    risk_pipeline=pipeline,
                    wall_clock=self._clock,
                ),
                options,
            )
        return (
            PaperShell(
                self._configuration,
                options=options,
                risk_pipeline=pipeline,
                wall_clock=self._clock,
            ),
            options,
        )

    def _kill_switch_pipeline(self) -> RiskPipeline | None:
        if self._mode == "paper" and self._snapshot is not None:
            return None
        return None

    def _session_options(self) -> PaperOptions:
        session = self._session
        assert session is not None
        return PaperOptions(
            run_id=self._run_id,
            initial_cash=self._settings.initial_cash,
            session_policy=PaperSessionPolicy.STOP_AT_CLOSE,
            session=session,
            idle_pace=self._housekeeping,
        )

    # -- pump -----------------------------------------------------------------

    def _pump(
        self, shell: PaperShell | ShadowShell, feed: PaperFeed
    ) -> PaperSessionResult:
        self._arm_cadences()
        try:
            if isinstance(shell, ShadowShell):
                return shell.run(feed).session
            return shell.run(feed)
        finally:
            feed.close()

    def _arm_cadences(self) -> None:
        now = self._clock.now()
        session = self._session
        assert session is not None
        market_deadline = (
            session.open_time if now < session.open_time else now
        ) + self._settings.market_wait
        self._market_deadline = market_deadline
        self._next_heartbeat = now + self._settings.heartbeat_cadence
        self._next_kill_switch_poll = now + self._settings.kill_switch_poll
        self._next_periodic_snapshot = now + self._settings.snapshot_cadence
        self._next_periodic_reconcile = now + self._settings.reconcile_cadence

    def _housekeeping(self) -> None:
        shell = self._shell
        if shell is None:
            return
        now = self._clock.now()
        result = _session_result(shell)
        market_deadline = self._market_deadline
        assert market_deadline is not None
        if result.market_event_count == 0 and now >= market_deadline:
            raise MarketWaitExceeded(
                f"{_MARKET_WAIT_PREFIX}: no market events by {_iso(market_deadline)}"
            )
        if self._stop_requested:
            return
        if now >= self._next_kill_switch_poll:
            self._next_kill_switch_poll = now + self._settings.kill_switch_poll
            if self._poll_kill_switch():
                return
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + self._settings.heartbeat_cadence
            self._journal_status(
                SupervisionPhase.RUNNING, "heartbeat", shell=shell, heartbeat=True
            )
        if isinstance(shell, PaperShell) and now >= self._next_periodic_snapshot:
            self._next_periodic_snapshot = now + self._settings.snapshot_cadence
            self._capture(shell, SnapshotTrigger.PERIODIC)
        if (
            self._reconciler is not None
            and isinstance(shell, PaperShell)
            and now >= self._next_periodic_reconcile
        ):
            self._next_periodic_reconcile = now + self._settings.reconcile_cadence
            self._periodic_reconcile(shell)
        self._sleep(self._settings.idle_pace.total_seconds())

    def _poll_kill_switch(self) -> bool:
        """Poll the remote kill switch; return whether the session must stop."""
        if self._poller is None:
            return False
        try:
            observation = self._poller.poll()
        except Exception as error:
            self._poll_failures += 1
            if self._poll_failures > self._settings.kill_switch_stale:
                raise RestartableFault(
                    f"{_RESTARTABLE_PREFIX}: kill-switch poll failed "
                    f"{self._poll_failures} consecutive times: {error}"
                ) from error
            return False
        self._poll_failures = 0
        if not observation.engaged:
            return False
        reason = observation.reason or "operator engaged"
        self._kill_switch.trip(reason=reason, actor=_KILL_SWITCH_ACTOR)
        shell = self._shell
        if shell is not None:
            shell.stop(f"{_KILL_SWITCH_PREFIX}: {reason}")
        self._transition(SupervisionPhase.RUNNING, f"kill-switch engaged: {reason}")
        return True

    def _capture(self, shell: PaperShell, trigger: SnapshotTrigger) -> None:
        assert self._options is not None
        try:
            snapshot = self._state_store.capture(
                shell,
                options=self._options,
                strategy_ids=[
                    instance.instance_id
                    for instance in self._configuration.strategy_instances
                ],
                trigger=trigger,
                sequence=self._snapshot_sequence,
            )
            self._snapshot_sequence += 1
            self._state_store.save(snapshot)
        except Exception as error:
            raise RestartableFault(
                f"{_RESTARTABLE_PREFIX}: snapshot save failed: {error}"
            ) from error
        self._transition(
            SupervisionPhase.RUNNING,
            f"{trigger.value} snapshot {snapshot.snapshot_id[:12]} saved",
        )

    def _periodic_reconcile(self, shell: PaperShell) -> None:
        assert self._reconciler is not None
        try:
            outcome = self._reconciler.reconcile(
                hub=shell.hub,
                trigger=ReconcileTrigger.PERIODIC,
                sequence=(self._reconciler.last_sequence or 0) + 1,
                observed_at=self._clock.now(),
            )
        except Exception as error:
            raise RestartableFault(
                f"{_RESTARTABLE_PREFIX}: periodic reconciliation failed: {error}"
            ) from error
        if outcome.decision == "abort":
            shell.stop(f"{_RECONCILE_PREFIX}: {outcome.diff_id}")
            self._transition(
                SupervisionPhase.RUNNING,
                f"{_RECONCILE_PREFIX}: diff {outcome.diff_id[:12]}",
            )

    # -- finish ---------------------------------------------------------------

    def _finish(self, result: PaperSessionResult) -> SupervisorOutcome:
        shell = self._shell
        assert shell is not None
        status = result.status
        reason = result.reason
        if status is PaperSessionStatus.AT_CLOSE:
            if isinstance(shell, PaperShell):
                self._capture(shell, SnapshotTrigger.SESSION_END)
            self._transition(SupervisionPhase.TEARDOWN, "session closed")
            return self._outcome(
                TraderExitCode.SUCCESS,
                SupervisionPhase.AT_CLOSE,
                reason,
                OutcomeKind.CLEAN,
                session=result,
            )
        if status is PaperSessionStatus.COMPLETED:
            if isinstance(shell, PaperShell):
                self._capture(shell, SnapshotTrigger.SESSION_END)
            self._transition(SupervisionPhase.TEARDOWN, "session completed")
            return self._outcome(
                TraderExitCode.SUCCESS,
                SupervisionPhase.COMPLETED,
                reason,
                OutcomeKind.CLEAN,
                session=result,
            )
        if status is PaperSessionStatus.STOPPED:
            if isinstance(shell, PaperShell):
                self._capture(shell, SnapshotTrigger.STOP)
            if reason is not None and reason.startswith(_KILL_SWITCH_PREFIX):
                self._transition(SupervisionPhase.HALTED, reason)
                return self._outcome(
                    TraderExitCode.KILL_SWITCH_HALTED,
                    SupervisionPhase.HALTED,
                    reason,
                    OutcomeKind.KILL_SWITCH,
                    session=result,
                )
            if reason is not None and reason.startswith(_RECONCILE_PREFIX):
                return self._fault_outcome(
                    SupervisionError(reason), TraderExitCode.FAILED, session=result
                )
            self._transition(SupervisionPhase.STOPPED, reason or "stop requested")
            return self._outcome(
                TraderExitCode.SUCCESS,
                SupervisionPhase.STOPPED,
                reason,
                OutcomeKind.CLEAN,
                session=result,
            )
        assert status is PaperSessionStatus.ABORTED
        if reason is not None and (
            reason.startswith(_RESTARTABLE_PREFIX)
            or reason.startswith(_MARKET_WAIT_PREFIX)
        ):
            return self._fault_outcome(
                SupervisionError(reason),
                TraderExitCode.RESTARTABLE_FAULT,
                session=result,
            )
        return self._fault_outcome(
            SupervisionError(reason or "session aborted"),
            TraderExitCode.FAILED,
            session=result,
        )

    # -- outcome plumbing -----------------------------------------------------

    def _fault_outcome(
        self,
        error: Exception,
        exit_code: TraderExitCode,
        session: PaperSessionResult | None = None,
    ) -> SupervisorOutcome:
        detail = str(error)
        shell = self._shell
        if shell is not None:
            try:
                if not shell.finalized:
                    shell.close()
            except Exception:  # noqa: BLE001 - teardown must not mask the fault
                pass
        self._transition(SupervisionPhase.FAULTED, detail)
        kind = (
            OutcomeKind.FAULT
            if exit_code
            in (
                TraderExitCode.RESTARTABLE_FAULT,
                TraderExitCode.FAILED,
                TraderExitCode.CRASH_LOOP,
                TraderExitCode.POLICY_HALTED,
            )
            else OutcomeKind.CLEAN
        )
        return self._outcome(exit_code, self._phase, detail, kind, session=session)

    def _outcome(
        self,
        exit_code: TraderExitCode,
        phase: SupervisionPhase,
        detail: str | None,
        kind: OutcomeKind,
        recovery: RecoveryReport | None = None,
        session: PaperSessionResult | None = None,
    ) -> SupervisorOutcome:
        outcome = SupervisorOutcome(
            run_id=self._run_id,
            exit_code=exit_code,
            phase=phase,
            detail=detail,
            recovery=self._recovery if recovery is None else recovery,
            session=session if session is not None else self._outcome_session,
        )
        try:
            self._journal.record_status(
                phase=phase,
                detail=detail,
                shell=self._shell,
                recovery=outcome.recovery,
            )
            self._journal.record_boot_outcome(kind, detail or phase.value)
        except Exception:  # noqa: BLE001 - exit codes must survive ledger loss
            if self._emit is not None:
                self._emit(
                    f"supervision ledger write failed during {phase.value}: "
                    "the restart budget is unbounded until the ledger heals"
                )
        return outcome

    def _transition(self, phase: SupervisionPhase, detail: str) -> None:
        self._phase = phase
        self._journal_status(phase, detail)

    def _journal_status(
        self,
        phase: SupervisionPhase,
        detail: str,
        *,
        shell: PaperShell | ShadowShell | None = None,
        heartbeat: bool = False,
    ) -> None:
        self._journal.record_status(
            phase=phase,
            detail=detail,
            shell=shell if shell is not None else self._shell,
            recovery=self._recovery,
            heartbeat=heartbeat,
        )
        if self._emit is not None and heartbeat:
            self._emit(
                f"heartbeat run={self._run_id} phase={self._phase.value} "
                f"at={_iso(self._clock.now())}"
            )

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            sleep(seconds)


def sleep(seconds: float) -> None:
    """Blocking pace used by the CLI composition; injected in tests."""
    import time

    time.sleep(seconds)


# --------------------------------------------------------------------------
# Live reorder boundary
# --------------------------------------------------------------------------

type MarketEvent = QuoteEvent | TradeEvent

FEED_LATENESS: timedelta = timedelta(seconds=2)
FEED_BUFFER_CAPACITY: int = 8192


class ReorderingInbox:
    """Kernel-facing reorder buffer over a live feed inbox.

    Live consolidated feeds interleave per-symbol streams, so provider frame
    order does not guarantee nondecreasing event timestamps across symbols,
    while the deterministic kernel requires nondecreasing
    ``(timestamp, sequence)`` delivery.  This buffer holds events inside a
    bounded lateness window and releases them in
    ``(timestamp, sequence, class)`` order once a later-arriving print can no
    longer precede them: an event is releasable when its timestamp is at or
    behind the high-water mark minus the lateness window, or immediately when
    the inner inbox is momentarily idle.  Events that arrive after their
    timestamp has already been passed are dropped beyond tolerance and
    counted in :attr:`dropped_late` rather than violating kernel ordering.
    When a burst fills the buffer with stamps tighter than the lateness
    window, the oldest buffered event is shed — also counted in
    :attr:`dropped_late` — so the boundary always makes progress and the
    kernel never stalls on the live feed.
    """

    __slots__ = (
        "_buffer",
        "_capacity",
        "_dropped_late",
        "_inner",
        "_lateness",
        "_watermark",
    )

    def __init__(
        self,
        inner: PaperInbox,
        *,
        lateness: timedelta = FEED_LATENESS,
        capacity: int = FEED_BUFFER_CAPACITY,
    ) -> None:
        if lateness < timedelta(0):
            raise ValueError("lateness must not be negative")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if not hasattr(inner, "pop"):
            raise TypeError("inner must provide pop()")
        self._inner = inner
        self._lateness = lateness
        self._capacity = capacity
        self._buffer: list[tuple[datetime, int, int, MarketEvent]] = []
        self._watermark: datetime | None = None
        self._dropped_late = 0

    @property
    def dropped_late(self) -> int:
        """Return how many beyond-tolerance late events were dropped."""
        return self._dropped_late

    def pop(self) -> MarketEvent | None:
        """Return the next in-order event, or ``None`` while idle."""
        while True:
            inner_idle = False
            while len(self._buffer) < self._capacity:
                event = self._inner.pop()
                if event is None:
                    inner_idle = True
                    break
                stamp = event.timestamp
                boundary = (
                    None
                    if self._watermark is None
                    else self._watermark - self._lateness
                )
                if boundary is not None and stamp < boundary:
                    self._dropped_late += 1
                    continue
                if self._watermark is None or stamp > self._watermark:
                    self._watermark = stamp
                heapq.heappush(
                    self._buffer,
                    (
                        stamp,
                        event.sequence,
                        EVENT_CLASS_PRIORITY[event.event_type],
                        event,
                    ),
                )
            if not self._buffer:
                return None
            boundary = (
                None if self._watermark is None else self._watermark - self._lateness
            )
            if boundary is None or self._buffer[0][0] <= boundary or inner_idle:
                return heapq.heappop(self._buffer)[3]
            if len(self._buffer) >= self._capacity:
                # A burst can fill the buffer with stamps tighter than the
                # lateness window, wedging the release condition forever.
                # Shed the oldest buffered event (counted as beyond
                # tolerance) so the boundary always makes progress and the
                # kernel never stalls on the live feed.
                heapq.heappop(self._buffer)
                self._dropped_late += 1
                continue


# --------------------------------------------------------------------------
# Kill-switch table adapter
# --------------------------------------------------------------------------

KILL_SWITCH_TABLE_PARTITION_KEY: str = "killswitch"
KILL_SWITCH_TABLE_ROW_KEY: str = "global"
KILL_SWITCH_TABLE_ENDPOINT_VARIABLE: str = "FCA_KILL_SWITCH_TABLE__ENDPOINT"
KILL_SWITCH_TABLE_KEY_VARIABLE: str = "FCA_KILL_SWITCH_TABLE__KEY"
KILL_SWITCH_TABLE_NAME_VARIABLE: str = "HARNESS_KILL_SWITCH_TABLE"
STORAGE_ACCOUNT_VARIABLE: str = "LAKE_STORAGE_ACCOUNT_NAME"
IDENTITY_CLIENT_VARIABLE: str = "AZURE_CLIENT_ID"
DEFAULT_KILL_SWITCH_TABLE: str = "killswitch"


class TableKillSwitchPoller:
    """Minimal Azure Table kill-switch source for the deployment boundary.

    The pinned contract: one table row at
    ``(PartitionKey='killswitch', RowKey='global')`` carries a boolean
    ``engaged`` property and an optional ``reason``.  A missing row is a
    disengaged switch; any other table error propagates to the supervisor,
    which treats repeated failures as a restartable fault rather than a
    silent proceed.

    Credentials resolve from the environment in two documented shapes: the
    ``FCA_KILL_SWITCH_TABLE__ENDPOINT``/``FCA_KILL_SWITCH_TABLE__KEY``
    pair (shared-key; local and test environments), or the deployment
    contract of managed-identity reads against the runtime storage account
    named by ``LAKE_STORAGE_ACCOUNT_NAME``.
    """

    __slots__ = ("_client",)

    def __init__(self, *, client: TableClient) -> None:
        if not isinstance(client, TableClient):
            raise TypeError("client must be an azure TableClient")
        self._client = client

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str] | None = None
    ) -> TableKillSwitchPoller | None:
        """Compose the poller from the documented environment contracts.

        Returns ``None`` when no contract is present, which leaves the
        supervisor on its in-process switch alone.
        """
        import os
        from urllib.parse import urlparse

        source = os.environ if environ is None else environ
        endpoint = source.get(KILL_SWITCH_TABLE_ENDPOINT_VARIABLE)
        key = source.get(KILL_SWITCH_TABLE_KEY_VARIABLE)
        table = source.get(KILL_SWITCH_TABLE_NAME_VARIABLE, DEFAULT_KILL_SWITCH_TABLE)
        if endpoint and key:
            account = urlparse(endpoint).hostname
            if account is None:
                raise SupervisionConfigurationError(
                    f"{KILL_SWITCH_TABLE_ENDPOINT_VARIABLE} must be a table "
                    "service endpoint URL"
                )
            name = account.split(".", maxsplit=1)[0]
            client = TableClient(
                endpoint=endpoint.rstrip("/"),
                table_name=table,
                credential=AzureNamedKeyCredential(name, key),
            )
            return cls(client=client)
        account_name = source.get(STORAGE_ACCOUNT_VARIABLE)
        if account_name:
            credential = DefaultAzureCredential(
                managed_identity_client_id=source.get(IDENTITY_CLIENT_VARIABLE) or None
            )
            client = TableClient(
                endpoint=f"https://{account_name}.table.core.windows.net",
                table_name=table,
                credential=credential,
            )
            return cls(client=client)
        return None

    def poll(self) -> KillSwitchObservation:
        """Return one observation from the pinned table contract."""
        from datetime import UTC, datetime

        try:
            entity = self._client.get_entity(
                partition_key=KILL_SWITCH_TABLE_PARTITION_KEY,
                row_key=KILL_SWITCH_TABLE_ROW_KEY,
            )
        except ResourceNotFoundError:
            return KillSwitchObservation(
                engaged=False, reason=None, at=datetime.now(UTC)
            )
        engaged = bool(entity.get("engaged", False))
        reason = entity.get("reason")
        return KillSwitchObservation(
            engaged=engaged,
            reason=reason if isinstance(reason, str) and reason else None,
            at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------
# Composition helpers
# --------------------------------------------------------------------------


def trading_session_for(
    clock: Clock,
    *,
    calendar: Callable[[object, object], Sequence[object]] | None = None,
) -> ExchangeSession | None:
    """Resolve the XNYS session covering the clock's date, if any.

    Returns the session whose window contains the clock instant, or the
    next session opening later the same date.  Returns ``None`` when the
    date has no exchange session (weekends and holidays).
    """
    from lake.contracts.sessions import xnys_sessions

    now = clock.now()
    today = now.date()
    sessions = xnys_sessions(today, today)
    for session in sessions:
        if session.open_time <= now < session.close_time:
            return session
    for session in sessions:
        if session.open_time > now:
            return session
    return None


__all__ = [
    "DEFAULT_KILL_SWITCH_TABLE",
    "FeedBoundaryError",
    "FORMAT_VERSION",
    "KillSwitchObservation",
    "KillSwitchPoller",
    "MarketWaitExceeded",
    "OutcomeKind",
    "ReorderingInbox",
    "RestartableFault",
    "SessionJournal",
    "SnapshotBrokerStatePort",
    "SupervisorOutcome",
    "SupervisionPhase",
    "SupervisorSettings",
    "SupervisionConfigurationError",
    "SupervisionError",
    "TableKillSwitchPoller",
    "TraderExitCode",
    "TraderSupervisor",
    "trading_session_for",
]
