"""Immutable single- and multi-leg order records."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import decimal, text
from harness.core.types.events import Event, EventType


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class OrderStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REPLACED = "replaced"


class ExecutionType(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REPLACED = "replaced"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class OrderLeg:
    """One signed leg in a multi-leg order."""

    symbol: Symbol
    side: Side
    quantity: Decimal
    ratio: Decimal = Decimal("1")
    leg_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(
            self, "quantity", decimal(self.quantity, "quantity", positive=True)
        )
        object.__setattr__(self, "ratio", decimal(self.ratio, "ratio", positive=True))
        if self.leg_id is not None:
            object.__setattr__(self, "leg_id", text(self.leg_id, "leg_id"))


@dataclass(frozen=True, slots=True)
class SingleLegOrder(Event):
    """A market, limit, stop, or stop-limit order for one symbol."""

    order_id: str
    symbol: Symbol
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None
    event_type: ClassVar[EventType] = EventType.ORDER

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(
            self, "quantity", decimal(self.quantity, "quantity", positive=True)
        )
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "time_in_force", TimeInForce(self.time_in_force))
        limit = (
            None
            if self.limit_price is None
            else decimal(self.limit_price, "limit_price", positive=True)
        )
        stop = (
            None
            if self.stop_price is None
            else decimal(self.stop_price, "stop_price", positive=True)
        )
        object.__setattr__(self, "limit_price", limit)
        object.__setattr__(self, "stop_price", stop)
        if self.client_order_id is not None:
            object.__setattr__(
                self, "client_order_id", text(self.client_order_id, "client_order_id")
            )
        valid_prices = {
            OrderType.MARKET: (False, False),
            OrderType.LIMIT: (True, False),
            OrderType.STOP: (False, True),
            OrderType.STOP_LIMIT: (True, True),
        }
        if (limit is not None, stop is not None) != valid_prices[self.order_type]:
            raise ValueError(
                f"{self.order_type.value} order has invalid limit_price/stop_price"
            )

    @property
    def type(self) -> OrderType:
        """Short order-type alias used by strategy code."""
        return OrderType(self.order_type)


@dataclass(frozen=True, slots=True)
class MultiLegOrder(Event):
    """An atomic order containing two or more independently signed legs."""

    order_id: str
    legs: tuple[OrderLeg, ...] | Sequence[OrderLeg]
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: str | None = None
    limit_price: Decimal | None = None
    event_type: ClassVar[EventType] = EventType.MULTI_LEG_ORDER

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        normalized_legs = tuple(self.legs)
        if len(normalized_legs) < 2 or not all(
            isinstance(leg, OrderLeg) for leg in normalized_legs
        ):
            raise ValueError("multi-leg order requires at least two OrderLeg values")
        object.__setattr__(self, "legs", normalized_legs)
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "time_in_force", TimeInForce(self.time_in_force))
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            raise ValueError("stop orders are not supported for multi-leg orders")
        if self.client_order_id is not None:
            object.__setattr__(
                self, "client_order_id", text(self.client_order_id, "client_order_id")
            )
        limit = (
            None
            if self.limit_price is None
            else decimal(self.limit_price, "limit_price", positive=True)
        )
        object.__setattr__(self, "limit_price", limit)
        if self.order_type is OrderType.LIMIT and limit is None:
            raise ValueError("limit multi-leg order requires a limit_price")
        if self.order_type is OrderType.MARKET and limit is not None:
            raise ValueError("market multi-leg order cannot have a limit_price")


Order = SingleLegOrder
OrderRequest = SingleLegOrder | MultiLegOrder


__all__ = [
    "ExecutionType",
    "MultiLegOrder",
    "Order",
    "OrderLeg",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Side",
    "SingleLegOrder",
    "TimeInForce",
]
