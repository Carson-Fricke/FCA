"""Atomic lifecycle and exact accounting for multi-leg order groups."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.orders import MultiLegOrder, OrderLeg, OrderType, Side
from harness.oms.state import OmsError

_ZERO = Decimal("0")


class GroupOrderType(StrEnum):
    """Price constraint applied to an atomic multi-leg order."""

    MARKET = "market"
    NET_DEBIT = "net_debit"
    NET_CREDIT = "net_credit"


class GroupStatus(StrEnum):
    """Lifecycle state shared by a group and every one of its legs."""

    NEW = "new"
    ACCEPTED = "accepted"
    EVALUATING = "evaluating"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


_TERMINAL_STATUSES = frozenset(
    {
        GroupStatus.FILLED,
        GroupStatus.CANCELED,
        GroupStatus.REJECTED,
        GroupStatus.EXPIRED,
    }
)


class GroupError(OmsError):
    """Base class for deterministic multi-leg OMS failures."""


class GroupTransitionError(GroupError):
    """Raised when an operation cannot follow the current group state."""


class GroupFillError(GroupError):
    """Raised when a candidate batch is not a complete valid group fill."""


class DuplicateGroupError(GroupError):
    """Raised when a group identifier has already been submitted."""


class UnknownGroupError(KeyError):
    """Raised when a group identifier is not tracked by this OMS."""


@dataclass(frozen=True, slots=True)
class GroupLegSnapshot:
    """Lifecycle and exact execution ledger for one ordered group leg."""

    index: int
    leg: OrderLeg
    status: GroupStatus
    filled_quantity: Decimal = _ZERO
    remaining_quantity: Decimal = _ZERO
    filled_notional: Decimal = _ZERO
    average_fill_price: Decimal | None = None
    fee: Decimal = _ZERO
    gross_cash: Decimal = _ZERO
    net_cash: Decimal = _ZERO
    position_quantity: Decimal = _ZERO
    fill: Fill | None = None

    @property
    def leg_id(self) -> str | None:
        """Return the optional venue-facing leg identifier."""
        return self.leg.leg_id

    @property
    def action(self) -> Side:
        """Return the buy or sell action for this leg."""
        return self.leg.side

    @property
    def ratio(self) -> Decimal:
        """Return the leg's ratio used for group net-price accounting."""
        return self.leg.ratio

    @property
    def quantity(self) -> Decimal:
        """Return the exact quantity that an atomic fill must execute."""
        return self.leg.quantity

    @property
    def symbol(self) -> Symbol:
        """Return the canonical symbol for this leg."""
        return self.leg.symbol


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    """Immutable lifecycle and accounting snapshot for one order group.

    ``net_price`` is ratio weighted and positive for a debit, negative for a
    credit. ``net_cash`` instead uses actual leg quantities and subtracts every
    supplied fee; a positive value is cash received by the account.
    """

    order: MultiLegOrder
    order_type: GroupOrderType
    status: GroupStatus
    legs: tuple[GroupLegSnapshot, ...]
    net_price: Decimal | None = None
    gross_cash: Decimal = _ZERO
    fees: Decimal = _ZERO
    net_cash: Decimal = _ZERO
    transition_count: int = 0
    reason: str | None = None

    @property
    def group_id(self) -> str:
        """Return the multi-leg order identifier."""
        return self.order.order_id

    @property
    def quantity(self) -> Decimal:
        """Return the common group quantity implied by every leg ratio."""
        first = self.legs[0]
        return first.quantity / first.ratio

    @property
    def is_active(self) -> bool:
        """Whether the group can accept another lifecycle operation."""
        return self.status not in _TERMINAL_STATUSES

    @property
    def is_terminal(self) -> bool:
        """Whether the group has reached an irreversible state."""
        return self.status in _TERMINAL_STATUSES

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Return fills in original leg order after an atomic execution."""
        return tuple(leg.fill for leg in self.legs if leg.fill is not None)

    @property
    def net_debit(self) -> Decimal:
        """Return the positive ratio-weighted debit, or zero for a credit."""
        if self.net_price is None:
            return _ZERO
        return max(self.net_price, _ZERO)

    @property
    def net_credit(self) -> Decimal:
        """Return the positive ratio-weighted credit, or zero for a debit."""
        if self.net_price is None:
            return _ZERO
        return max(-self.net_price, _ZERO)


class MultiLegOrderGroup:
    """Apply an all-or-nothing lifecycle to one immutable multi-leg order."""

    __slots__ = ("_fills", "_snapshot")

    def __init__(
        self,
        order: MultiLegOrder,
        order_type: GroupOrderType | str | None = None,
    ) -> None:
        if not isinstance(order, MultiLegOrder):
            raise TypeError("order must be a MultiLegOrder")
        resolved_type = self._resolve_order_type(order, order_type)
        leg_ids = [leg.leg_id for leg in order.legs if leg.leg_id is not None]
        if len(leg_ids) != len(set(leg_ids)):
            raise GroupError("leg_id values must be unique within a group")
        group_quantity = order.legs[0].quantity / order.legs[0].ratio
        if any(leg.quantity / leg.ratio != group_quantity for leg in order.legs[1:]):
            raise GroupError("leg quantities must be proportional to their ratios")

        self._snapshot = GroupSnapshot(
            order=order,
            order_type=resolved_type,
            status=GroupStatus.NEW,
            legs=tuple(
                GroupLegSnapshot(
                    index=index,
                    leg=leg,
                    status=GroupStatus.NEW,
                    remaining_quantity=leg.quantity,
                )
                for index, leg in enumerate(order.legs)
            ),
        )
        self._fills: tuple[Fill, ...] = ()

    @property
    def snapshot(self) -> GroupSnapshot:
        """Return the current immutable group snapshot."""
        return self._snapshot

    @property
    def order(self) -> MultiLegOrder:
        """Return the immutable multi-leg order request."""
        return self._snapshot.order

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Return the committed fills in original leg order."""
        return self._fills

    def accept(self) -> GroupSnapshot:
        """Acknowledge a locally new group."""
        self._require_status("accept", GroupStatus.NEW)
        return self._transition(GroupStatus.ACCEPTED)

    def begin_evaluation(self) -> GroupSnapshot:
        """Mark an accepted group as being considered for atomic execution."""
        self._require_status("begin evaluation", GroupStatus.ACCEPTED)
        return self._transition(GroupStatus.EVALUATING)

    def end_evaluation(self) -> GroupSnapshot:
        """Return an unfilled evaluated group to its accepted working state."""
        self._require_status("end evaluation", GroupStatus.EVALUATING)
        return self._transition(GroupStatus.ACCEPTED)

    def fill(self, fills: Sequence[Fill]) -> GroupSnapshot:
        """Validate and atomically commit exactly one complete fill per leg."""
        self._require_status("fill", GroupStatus.EVALUATING)
        candidate = tuple(fills)
        if not all(isinstance(fill, Fill) for fill in candidate):
            raise TypeError("fills must contain Fill values")

        ordered_fills = self._ordered_complete_fills(candidate)
        next_snapshot = self._filled_snapshot(ordered_fills)
        self._fills = ordered_fills
        self._snapshot = next_snapshot
        return next_snapshot

    def cancel(self, reason: str | None = None) -> GroupSnapshot:
        """Cancel an active group and propagate cancellation to every leg."""
        self._require_active("cancel")
        return self._transition(GroupStatus.CANCELED, reason)

    def reject(self, reason: str | None = None) -> GroupSnapshot:
        """Reject an active group and propagate rejection to every leg."""
        self._require_active("reject")
        return self._transition(GroupStatus.REJECTED, reason)

    def expire(self, reason: str | None = None) -> GroupSnapshot:
        """Expire a working group and propagate expiry to every leg."""
        self._require_status(
            "expire",
            GroupStatus.ACCEPTED,
            GroupStatus.EVALUATING,
        )
        return self._transition(GroupStatus.EXPIRED, reason)

    def _transition(
        self, status: GroupStatus, reason: str | None = None
    ) -> GroupSnapshot:
        normalized_reason = self._normalize_reason(reason)
        snapshot = replace(
            self._snapshot,
            status=status,
            legs=tuple(replace(leg, status=status) for leg in self._snapshot.legs),
            transition_count=self._snapshot.transition_count + 1,
            reason=normalized_reason,
        )
        self._snapshot = snapshot
        return snapshot

    def _ordered_complete_fills(self, candidate: tuple[Fill, ...]) -> tuple[Fill, ...]:
        legs = self._snapshot.legs
        if len(candidate) != len(legs):
            raise GroupFillError(
                "atomic group fill requires exactly one fill for every leg"
            )
        fill_ids = [fill.fill_id for fill in candidate]
        if len(fill_ids) != len(set(fill_ids)):
            raise GroupFillError("fill_id values must be unique within a group fill")
        if any(fill.order_id != self._snapshot.group_id for fill in candidate):
            raise GroupFillError("every fill must reference the group order_id")

        unmatched = list(candidate)
        ordered: list[Fill] = []
        for leg in legs:
            match = next(
                (
                    index
                    for index, fill in enumerate(unmatched)
                    if fill.symbol == leg.leg.symbol
                    and fill.side is leg.action
                    and fill.quantity == leg.quantity
                ),
                None,
            )
            if match is None:
                raise GroupFillError(
                    f"leg {leg.index} requires one exact symbol/action/quantity fill; "
                    "partial group fills are prohibited"
                )
            ordered.append(unmatched.pop(match))
        return tuple(ordered)

    def _filled_snapshot(self, fills: tuple[Fill, ...]) -> GroupSnapshot:
        net_price = _ZERO
        gross_cash = _ZERO
        fees = _ZERO
        filled_legs: list[GroupLegSnapshot] = []
        for current, fill in zip(self._snapshot.legs, fills, strict=True):
            notional = fill.quantity * fill.price
            if current.action is Side.BUY:
                leg_gross_cash = -notional
                position_quantity = fill.quantity
                net_price += fill.price * current.ratio
            else:
                leg_gross_cash = notional
                position_quantity = -fill.quantity
                net_price -= fill.price * current.ratio
            leg_net_cash = leg_gross_cash - fill.fee
            gross_cash += leg_gross_cash
            fees += fill.fee
            filled_legs.append(
                replace(
                    current,
                    status=GroupStatus.FILLED,
                    filled_quantity=fill.quantity,
                    remaining_quantity=_ZERO,
                    filled_notional=notional,
                    average_fill_price=fill.price,
                    fee=fill.fee,
                    gross_cash=leg_gross_cash,
                    net_cash=leg_net_cash,
                    position_quantity=position_quantity,
                    fill=fill,
                )
            )

        self._validate_net_limit(net_price)
        return replace(
            self._snapshot,
            status=GroupStatus.FILLED,
            legs=tuple(filled_legs),
            net_price=net_price,
            gross_cash=gross_cash,
            fees=fees,
            net_cash=gross_cash - fees,
            transition_count=self._snapshot.transition_count + 1,
            reason=None,
        )

    def _validate_net_limit(self, net_price: Decimal) -> None:
        order_type = self._snapshot.order_type
        limit = self._snapshot.order.limit_price
        if order_type is GroupOrderType.MARKET:
            return
        assert limit is not None
        if order_type is GroupOrderType.NET_DEBIT:
            if net_price <= _ZERO:
                raise GroupFillError("net-debit group must execute for a debit")
            if net_price > limit:
                raise GroupFillError("net-debit fill exceeds the group limit_price")
            return
        if net_price >= _ZERO:
            raise GroupFillError("net-credit group must execute for a credit")
        if -net_price < limit:
            raise GroupFillError("net-credit fill is below the group limit_price")

    def _require_active(self, operation: str) -> None:
        if not self._snapshot.is_active:
            raise GroupTransitionError(
                f"cannot {operation} group in {self._snapshot.status.value} state"
            )

    def _require_status(self, operation: str, *statuses: GroupStatus) -> None:
        if self._snapshot.status not in statuses:
            raise GroupTransitionError(
                f"cannot {operation} group in {self._snapshot.status.value} state"
            )

    @staticmethod
    def _normalize_reason(reason: str | None) -> str | None:
        if reason is None:
            return None
        if not isinstance(reason, str):
            raise TypeError("reason must be a string or None")
        normalized = reason.strip()
        if not normalized:
            raise ValueError("reason must not be empty")
        return normalized

    @staticmethod
    def _resolve_order_type(
        order: MultiLegOrder, order_type: GroupOrderType | str | None
    ) -> GroupOrderType:
        if order_type is None:
            if order.order_type is OrderType.MARKET:
                return GroupOrderType.MARKET
            raise GroupError(
                "limit multi-leg order requires NET_DEBIT or NET_CREDIT order_type"
            )
        resolved = GroupOrderType(order_type)
        if order.order_type is OrderType.MARKET:
            if resolved is not GroupOrderType.MARKET:
                raise GroupError("market multi-leg order requires MARKET order_type")
        elif resolved is GroupOrderType.MARKET:
            raise GroupError("limit multi-leg order requires a net price order_type")
        return resolved


class MultiLegOMS:
    """Registry and deterministic operation router for atomic order groups."""

    __slots__ = ("_groups", "_used_fill_ids", "_used_group_ids")

    def __init__(self) -> None:
        self._groups: dict[str, MultiLegOrderGroup] = {}
        self._used_group_ids: set[str] = set()
        self._used_fill_ids: set[str] = set()

    def submit(
        self,
        order: MultiLegOrder,
        order_type: GroupOrderType | str | None = None,
    ) -> GroupSnapshot:
        """Register a validated local group in ``NEW`` state."""
        if not isinstance(order, MultiLegOrder):
            raise TypeError("order must be a MultiLegOrder")
        if order.order_id in self._used_group_ids:
            raise DuplicateGroupError(
                f"group order_id {order.order_id!r} was already used"
            )
        group = MultiLegOrderGroup(order, order_type)
        self._groups[order.order_id] = group
        self._used_group_ids.add(order.order_id)
        return group.snapshot

    def accept(self, group_id: str) -> GroupSnapshot:
        """Acknowledge a locally new group."""
        return self._group(group_id).accept()

    def begin_evaluation(self, group_id: str) -> GroupSnapshot:
        """Begin one atomic matching evaluation."""
        return self._group(group_id).begin_evaluation()

    def end_evaluation(self, group_id: str) -> GroupSnapshot:
        """End an evaluation that found no complete fill."""
        return self._group(group_id).end_evaluation()

    def fill(self, group_id: str, fills: Sequence[Fill]) -> GroupSnapshot:
        """Atomically commit a complete fill batch to an evaluating group."""
        candidate = tuple(fills)
        duplicate_ids = {
            fill.fill_id
            for fill in candidate
            if isinstance(fill, Fill) and fill.fill_id in self._used_fill_ids
        }
        if duplicate_ids:
            duplicate = min(duplicate_ids)
            raise GroupFillError(f"fill_id {duplicate!r} was already used")
        snapshot = self._group(group_id).fill(candidate)
        self._used_fill_ids.update(fill.fill_id for fill in snapshot.fills)
        return snapshot

    def cancel(self, group_id: str, reason: str | None = None) -> GroupSnapshot:
        """Cancel a group, including one currently under evaluation."""
        return self._group(group_id).cancel(reason)

    def reject(self, group_id: str, reason: str | None = None) -> GroupSnapshot:
        """Reject an active group."""
        return self._group(group_id).reject(reason)

    def expire(self, group_id: str, reason: str | None = None) -> GroupSnapshot:
        """Expire a working group."""
        return self._group(group_id).expire(reason)

    def state(self, group_id: str) -> GroupSnapshot:
        """Return the current immutable state for a group."""
        return self._group(group_id).snapshot

    def fills(self, group_id: str) -> tuple[Fill, ...]:
        """Return committed fills in original leg order."""
        return self._group(group_id).fills

    def active_states(self) -> tuple[GroupSnapshot, ...]:
        """Return active groups in deterministic submission order."""
        return tuple(
            group.snapshot
            for group in self._groups.values()
            if group.snapshot.is_active
        )

    def _group(self, group_id: str) -> MultiLegOrderGroup:
        try:
            return self._groups[group_id]
        except KeyError as error:
            raise UnknownGroupError(group_id) from error


__all__ = [
    "DuplicateGroupError",
    "GroupError",
    "GroupFillError",
    "GroupLegSnapshot",
    "GroupOrderType",
    "GroupSnapshot",
    "GroupStatus",
    "GroupTransitionError",
    "MultiLegOMS",
    "MultiLegOrderGroup",
    "UnknownGroupError",
]
