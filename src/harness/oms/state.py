"""Execution-report-driven state and exact ledgers for one order."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from harness.core.types.accounting import ExecutionReport
from harness.core.types.orders import (
    ExecutionType,
    OrderStatus,
    SingleLegOrder,
    TimeInForce,
)

_ZERO = Decimal("0")

_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
    }
)

_EXECUTION_STATUSES = {
    ExecutionType.ACKNOWLEDGED: frozenset({OrderStatus.ACCEPTED}),
    ExecutionType.PARTIAL_FILL: frozenset({OrderStatus.PARTIALLY_FILLED}),
    ExecutionType.FILL: frozenset({OrderStatus.FILLED}),
    ExecutionType.CANCELED: frozenset({OrderStatus.CANCELED}),
    ExecutionType.REJECTED: frozenset({OrderStatus.REJECTED}),
    ExecutionType.EXPIRED: frozenset({OrderStatus.EXPIRED}),
    ExecutionType.REPLACED: frozenset({OrderStatus.REPLACED}),
    ExecutionType.STATUS: frozenset(
        {
            OrderStatus.NEW,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }
    ),
}

# Transitions are keyed by execution type as well as status. This prevents a
# second acknowledgement from masquerading as an accepted-status heartbeat.
_ALLOWED_TRANSITIONS = frozenset(
    {
        (OrderStatus.NEW, ExecutionType.STATUS, OrderStatus.NEW),
        (OrderStatus.NEW, ExecutionType.ACKNOWLEDGED, OrderStatus.ACCEPTED),
        (OrderStatus.NEW, ExecutionType.REJECTED, OrderStatus.REJECTED),
        (OrderStatus.ACCEPTED, ExecutionType.STATUS, OrderStatus.ACCEPTED),
        (
            OrderStatus.ACCEPTED,
            ExecutionType.PARTIAL_FILL,
            OrderStatus.PARTIALLY_FILLED,
        ),
        (OrderStatus.ACCEPTED, ExecutionType.FILL, OrderStatus.FILLED),
        (
            OrderStatus.ACCEPTED,
            ExecutionType.STATUS,
            OrderStatus.CANCEL_PENDING,
        ),
        (OrderStatus.ACCEPTED, ExecutionType.CANCELED, OrderStatus.CANCELED),
        (OrderStatus.ACCEPTED, ExecutionType.EXPIRED, OrderStatus.EXPIRED),
        (OrderStatus.ACCEPTED, ExecutionType.REPLACED, OrderStatus.REPLACED),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.STATUS,
            OrderStatus.PARTIALLY_FILLED,
        ),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            OrderStatus.PARTIALLY_FILLED,
        ),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.FILL,
            OrderStatus.FILLED,
        ),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.STATUS,
            OrderStatus.CANCEL_PENDING,
        ),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.CANCELED,
            OrderStatus.CANCELED,
        ),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.EXPIRED,
            OrderStatus.EXPIRED,
        ),
        (
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.REPLACED,
            OrderStatus.REPLACED,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.STATUS,
            OrderStatus.CANCEL_PENDING,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.STATUS,
            OrderStatus.ACCEPTED,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.STATUS,
            OrderStatus.PARTIALLY_FILLED,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.PARTIAL_FILL,
            OrderStatus.PARTIALLY_FILLED,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.FILL,
            OrderStatus.FILLED,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.CANCELED,
            OrderStatus.CANCELED,
        ),
        (
            OrderStatus.CANCEL_PENDING,
            ExecutionType.EXPIRED,
            OrderStatus.EXPIRED,
        ),
    }
)


class OmsError(ValueError):
    """Base class for deterministic OMS validation failures."""


class IllegalTransitionError(OmsError):
    """Raised when an execution report cannot follow the current state."""


class ExecutionLedgerError(OmsError):
    """Raised when cumulative or last-fill report values are inconsistent."""


class DuplicateExecutionReportError(OmsError):
    """Raised when a report identifier is applied more than once."""


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    """Immutable lifecycle and exact execution ledger for one order."""

    order: SingleLegOrder
    status: OrderStatus
    filled_quantity: Decimal
    remaining_quantity: Decimal
    filled_notional: Decimal
    average_fill_price: Decimal | None
    report_count: int
    last_report: ExecutionReport | None

    @property
    def order_id(self) -> str:
        """Return the tracked order identifier."""
        return self.order.order_id

    @property
    def is_active(self) -> bool:
        """Whether further lifecycle reports may be accepted."""
        return self.status not in _TERMINAL_STATUSES

    @property
    def is_terminal(self) -> bool:
        """Whether the order has reached an irreversible state."""
        return self.status in _TERMINAL_STATUSES

    @property
    def time_in_force(self) -> TimeInForce:
        """Return the order's time-in-force policy."""
        return self.order.time_in_force


class OrderStateMachine:
    """Apply validated execution reports to one single-leg order.

    Construction establishes only the local ``NEW`` state. After that, the
    machine has one mutating entrypoint: :meth:`apply`.
    """

    __slots__ = ("_report_ids", "_reports", "_snapshot")

    def __init__(self, order: SingleLegOrder) -> None:
        if not isinstance(order, SingleLegOrder):
            raise TypeError("order must be a SingleLegOrder")
        self._snapshot = OrderSnapshot(
            order=order,
            status=OrderStatus.NEW,
            filled_quantity=_ZERO,
            remaining_quantity=order.quantity,
            filled_notional=_ZERO,
            average_fill_price=None,
            report_count=0,
            last_report=None,
        )
        self._reports: list[ExecutionReport] = []
        self._report_ids: set[str] = set()

    @property
    def snapshot(self) -> OrderSnapshot:
        """Return the current immutable snapshot."""
        return self._snapshot

    @property
    def order(self) -> SingleLegOrder:
        """Return the immutable order request."""
        return self._snapshot.order

    @property
    def reports(self) -> tuple[ExecutionReport, ...]:
        """Return accepted reports in application order."""
        return tuple(self._reports)

    def apply(self, report: ExecutionReport) -> OrderSnapshot:
        """Validate and atomically apply one execution report."""
        if not isinstance(report, ExecutionReport):
            raise TypeError("report must be an ExecutionReport")
        if report.report_id in self._report_ids:
            raise DuplicateExecutionReportError(
                f"execution report {report.report_id!r} was already applied"
            )

        next_snapshot = self._next_snapshot(report)
        self._reports.append(report)
        self._report_ids.add(report.report_id)
        self._snapshot = next_snapshot
        return next_snapshot

    def _next_snapshot(self, report: ExecutionReport) -> OrderSnapshot:
        current = self._snapshot
        if report.order_id != current.order.order_id:
            raise ExecutionLedgerError(
                f"report order_id {report.order_id!r} does not match "
                f"{current.order.order_id!r}"
            )
        if report.order_quantity != current.order.quantity:
            raise ExecutionLedgerError(
                "report order_quantity does not match the submitted order"
            )
        if report.status not in _EXECUTION_STATUSES[report.execution_type]:
            raise IllegalTransitionError(
                f"{report.execution_type.value} report cannot declare "
                f"{report.status.value}"
            )

        transition = (current.status, report.execution_type, report.status)
        if transition not in _ALLOWED_TRANSITIONS:
            raise IllegalTransitionError(
                f"illegal order transition {current.status.value} -> "
                f"{report.status.value} via {report.execution_type.value}"
            )

        filled_notional = self._validate_ledger(report)
        return replace(
            current,
            status=report.status,
            filled_quantity=report.filled_quantity,
            remaining_quantity=report.remaining_quantity,
            filled_notional=filled_notional,
            average_fill_price=report.average_fill_price,
            report_count=current.report_count + 1,
            last_report=report,
        )

    def _validate_ledger(self, report: ExecutionReport) -> Decimal:
        current = self._snapshot
        is_fill = report.execution_type in {
            ExecutionType.PARTIAL_FILL,
            ExecutionType.FILL,
        }

        if is_fill:
            if report.last_fill_quantity <= _ZERO:
                raise ExecutionLedgerError("fill report requires last_fill_quantity")
            if report.last_fill_price is None:
                raise ExecutionLedgerError("fill report requires last_fill_price")
            expected_filled = current.filled_quantity + report.last_fill_quantity
            if report.filled_quantity != expected_filled:
                raise ExecutionLedgerError(
                    "filled_quantity must increase by last_fill_quantity"
                )
            filled_notional = (
                current.filled_notional
                + report.last_fill_quantity * report.last_fill_price
            )
        else:
            if report.last_fill_quantity != _ZERO:
                raise ExecutionLedgerError(
                    "non-fill report cannot contain last_fill_quantity"
                )
            if report.last_fill_price is not None:
                raise ExecutionLedgerError(
                    "non-fill report cannot contain last_fill_price"
                )
            if report.filled_quantity != current.filled_quantity:
                raise ExecutionLedgerError(
                    "non-fill report cannot change filled_quantity"
                )
            filled_notional = current.filled_notional

        expected_remaining = current.order.quantity - report.filled_quantity
        if report.remaining_quantity != expected_remaining:
            raise ExecutionLedgerError(
                "remaining_quantity does not match the exact order ledger"
            )

        if report.filled_quantity == _ZERO:
            if report.average_fill_price is not None:
                raise ExecutionLedgerError(
                    "unfilled order cannot have average_fill_price"
                )
        else:
            expected_average = filled_notional / report.filled_quantity
            if report.average_fill_price != expected_average:
                raise ExecutionLedgerError(
                    "average_fill_price does not match the exact fill ledger"
                )

        if report.status is OrderStatus.PARTIALLY_FILLED and not (
            _ZERO < report.filled_quantity < current.order.quantity
        ):
            raise ExecutionLedgerError(
                "partially_filled status requires an incomplete positive fill"
            )
        if (
            report.status is OrderStatus.FILLED
            and report.filled_quantity != current.order.quantity
        ):
            raise ExecutionLedgerError(
                "filled status requires the complete order quantity"
            )
        if (
            report.status
            in {
                OrderStatus.NEW,
                OrderStatus.ACCEPTED,
                OrderStatus.REJECTED,
            }
            and report.filled_quantity != _ZERO
        ):
            raise ExecutionLedgerError(
                f"{report.status.value} status cannot have filled quantity"
            )
        return filled_notional


__all__ = [
    "DuplicateExecutionReportError",
    "ExecutionLedgerError",
    "IllegalTransitionError",
    "OmsError",
    "OrderSnapshot",
    "OrderStateMachine",
]
