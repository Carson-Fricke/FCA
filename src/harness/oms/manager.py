"""Single-leg order registry, replacement tracking, and expiry policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from harness.core.types.accounting import ExecutionReport
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    SingleLegOrder,
    TimeInForce,
)
from harness.oms.state import OmsError, OrderSnapshot, OrderStateMachine


class UnknownOrderError(KeyError):
    """Raised when an order identifier is not tracked by this OMS."""


class DuplicateOrderError(OmsError):
    """Raised when an order identifier has already been used or reserved."""


class ReplaceError(OmsError):
    """Raised when a replacement request or report is inconsistent."""


class ReplaceStatus(StrEnum):
    """Lifecycle of an atomic replacement request."""

    PENDING = "pending"
    APPLIED = "applied"
    ABORTED = "aborted"


class SessionAction(StrEnum):
    """Action required for an active order at a session boundary."""

    EXPIRE = "expire"
    CARRY = "carry"


@dataclass(frozen=True, slots=True)
class ReplaceRecord:
    """A reserved replacement and its report-confirmed outcome."""

    original_order_id: str
    replacement: SingleLegOrder
    status: ReplaceStatus = ReplaceStatus.PENDING
    report_id: str | None = None

    @property
    def replacement_order_id(self) -> str:
        """Return the reserved replacement identifier."""
        return self.replacement.order_id


@dataclass(frozen=True, slots=True)
class SessionDirective:
    """A deterministic session-boundary policy decision."""

    order_id: str
    action: SessionAction


class SingleLegOMS:
    """Track single-leg orders using execution reports as the mutation path."""

    __slots__ = (
        "_machines",
        "_replaced_by",
        "_replacements",
        "_replaces",
        "_used_order_ids",
    )

    def __init__(self) -> None:
        self._machines: dict[str, OrderStateMachine] = {}
        self._used_order_ids: set[str] = set()
        self._replacements: dict[str, ReplaceRecord] = {}
        self._replaced_by: dict[str, str] = {}
        self._replaces: dict[str, str] = {}

    def submit(self, order: SingleLegOrder) -> OrderSnapshot:
        """Register a validated local order in ``NEW`` state."""
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be a SingleLegOrder")
        if order.order_id in self._used_order_ids:
            raise DuplicateOrderError(f"order_id {order.order_id!r} was already used")
        machine = OrderStateMachine(order)
        self._machines[order.order_id] = machine
        self._used_order_ids.add(order.order_id)
        return machine.snapshot

    def apply(self, report: ExecutionReport) -> OrderSnapshot:
        """Route one report and atomically confirm any replacement."""
        if not isinstance(report, ExecutionReport):
            raise TypeError("report must be an ExecutionReport")
        machine = self._machine(report.order_id)

        if report.execution_type is ExecutionType.REPLACED:
            record = self._replacements.get(report.order_id)
            if record is None or record.status is not ReplaceStatus.PENDING:
                raise ReplaceError(
                    f"order {report.order_id!r} has no pending replacement"
                )
            # Construct first so all replacement validation precedes the old
            # order's terminal transition. Remaining dictionary writes cannot
            # fail under the synchronous, single-threaded kernel contract.
            replacement_machine = OrderStateMachine(record.replacement)
            snapshot = machine.apply(report)
            replacement_id = record.replacement_order_id
            self._machines[replacement_id] = replacement_machine
            self._replaced_by[report.order_id] = replacement_id
            self._replaces[replacement_id] = report.order_id
            self._replacements[report.order_id] = replace(
                record,
                status=ReplaceStatus.APPLIED,
                report_id=report.report_id,
            )
            return snapshot

        snapshot = machine.apply(report)
        record = self._replacements.get(report.order_id)
        if (
            record is not None
            and record.status is ReplaceStatus.PENDING
            and snapshot.is_terminal
        ):
            self._replacements[report.order_id] = replace(
                record,
                status=ReplaceStatus.ABORTED,
                report_id=report.report_id,
            )
        return snapshot

    def request_replace(
        self, original_order_id: str, replacement: SingleLegOrder
    ) -> ReplaceRecord:
        """Reserve a replacement without changing either order's lifecycle.

        The replacement becomes visible as a ``NEW`` order only when a valid
        ``REPLACED`` execution report terminates the original order.
        """
        if not isinstance(replacement, SingleLegOrder):
            raise TypeError("replacement must be a SingleLegOrder")
        original = self._machine(original_order_id).snapshot
        if original.status not in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise ReplaceError(f"cannot replace order in {original.status.value} state")
        if original_order_id in self._replacements:
            raise ReplaceError(f"order {original_order_id!r} was already replaced")
        if replacement.order_id in self._used_order_ids:
            raise DuplicateOrderError(
                f"order_id {replacement.order_id!r} was already used"
            )
        if replacement.symbol != original.order.symbol:
            raise ReplaceError("replacement cannot change symbol")
        if replacement.side is not original.order.side:
            raise ReplaceError("replacement cannot change side")

        record = ReplaceRecord(original_order_id, replacement)
        self._replacements[original_order_id] = record
        # Reservation prevents another submit or replacement from racing this
        # atomic operation, even before venue confirmation arrives.
        self._used_order_ids.add(replacement.order_id)
        return record

    def state(self, order_id: str) -> OrderSnapshot:
        """Return an order's current immutable state."""
        return self._machine(order_id).snapshot

    def reports(self, order_id: str) -> tuple[ExecutionReport, ...]:
        """Return all accepted reports for an order."""
        return self._machine(order_id).reports

    def replacement(self, original_order_id: str) -> ReplaceRecord | None:
        """Return replacement tracking for an original order, if present."""
        return self._replacements.get(original_order_id)

    def replaced_by(self, order_id: str) -> str | None:
        """Return the report-confirmed replacement order identifier."""
        return self._replaced_by.get(order_id)

    def replaces(self, order_id: str) -> str | None:
        """Return the order identifier replaced by this order."""
        return self._replaces.get(order_id)

    def replacement_chain(self, order_id: str) -> tuple[str, ...]:
        """Return the confirmed replacement chain containing ``order_id``."""
        self._machine(order_id)
        first = order_id
        while first in self._replaces:
            first = self._replaces[first]
        chain = [first]
        while chain[-1] in self._replaced_by:
            chain.append(self._replaced_by[chain[-1]])
        return tuple(chain)

    def active_states(self) -> tuple[OrderSnapshot, ...]:
        """Return active orders in deterministic registration order."""
        return tuple(
            machine.snapshot
            for machine in self._machines.values()
            if machine.snapshot.is_active
        )

    def session_directives(self) -> tuple[SessionDirective, ...]:
        """Describe session-close handling without mutating order state.

        Day and still-open IOC orders require an expiry report. GTC orders are
        deliberately carried with no synthetic state transition. Locally new
        orders are not yet venue-working and receive no session directive.
        """
        return tuple(
            SessionDirective(
                snapshot.order_id,
                SessionAction.CARRY
                if snapshot.time_in_force is TimeInForce.GTC
                else SessionAction.EXPIRE,
            )
            for snapshot in self.active_states()
            if snapshot.status is not OrderStatus.NEW
        )

    def ioc_expiry_directives(self) -> tuple[SessionDirective, ...]:
        """Return IOC orders requiring expiry after a matching opportunity."""
        return tuple(
            SessionDirective(snapshot.order_id, SessionAction.EXPIRE)
            for snapshot in self.active_states()
            if snapshot.time_in_force is TimeInForce.IOC
            and snapshot.status is not OrderStatus.NEW
        )

    def apply_session_expiry(self, report: ExecutionReport) -> OrderSnapshot:
        """Apply a Day/IOC session expiry report, refusing to expire GTC."""
        self._validate_expiry_report(report)
        snapshot = self.state(report.order_id)
        if snapshot.time_in_force is TimeInForce.GTC:
            raise OmsError("GTC order carries across session boundaries")
        return self.apply(report)

    def apply_ioc_expiry(self, report: ExecutionReport) -> OrderSnapshot:
        """Apply the expiry report emitted after an IOC matching opportunity."""
        self._validate_expiry_report(report)
        snapshot = self.state(report.order_id)
        if snapshot.time_in_force is not TimeInForce.IOC:
            raise OmsError("IOC expiry requires an IOC order")
        return self.apply(report)

    def _machine(self, order_id: str) -> OrderStateMachine:
        try:
            return self._machines[order_id]
        except KeyError as error:
            raise UnknownOrderError(order_id) from error

    @staticmethod
    def _validate_expiry_report(report: ExecutionReport) -> None:
        if not isinstance(report, ExecutionReport):
            raise TypeError("report must be an ExecutionReport")
        if (
            report.execution_type is not ExecutionType.EXPIRED
            or report.status is not OrderStatus.EXPIRED
        ):
            raise OmsError("expiry policy requires an expired execution report")


__all__ = [
    "DuplicateOrderError",
    "ReplaceError",
    "ReplaceRecord",
    "ReplaceStatus",
    "SessionAction",
    "SessionDirective",
    "SingleLegOMS",
    "UnknownOrderError",
]
