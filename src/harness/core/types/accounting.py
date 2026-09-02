"""Immutable execution, position, book, and account records."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import ClassVar

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import (
    decimal,
    exact_decimal,
    nonnegative,
    optional_decimal,
    optional_positive,
    text,
)
from harness.core.types.events import Event, EventType
from harness.core.types.orders import ExecutionType, OrderStatus, Side

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ExecutionReport(Event):
    """A venue/OMS report; fills are represented by :class:`Fill`."""

    report_id: str
    order_id: str
    status: OrderStatus
    execution_type: ExecutionType
    order_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal | None = None
    last_fill_quantity: Decimal = _ZERO
    last_fill_price: Decimal | None = None
    text: str | None = None
    event_type: ClassVar[EventType] = EventType.EXECUTION_REPORT

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "report_id", text(self.report_id, "report_id"))
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "status", OrderStatus(self.status))
        object.__setattr__(self, "execution_type", ExecutionType(self.execution_type))
        order_quantity = decimal(self.order_quantity, "order_quantity", positive=True)
        filled_quantity = nonnegative(self.filled_quantity, "filled_quantity")
        remaining_quantity = nonnegative(self.remaining_quantity, "remaining_quantity")
        if filled_quantity > order_quantity:
            raise ValueError("filled_quantity cannot exceed order_quantity")
        if filled_quantity + remaining_quantity != order_quantity:
            raise ValueError(
                "filled_quantity plus remaining_quantity must equal order_quantity"
            )
        object.__setattr__(self, "order_quantity", order_quantity)
        object.__setattr__(self, "filled_quantity", filled_quantity)
        object.__setattr__(self, "remaining_quantity", remaining_quantity)
        object.__setattr__(
            self,
            "average_fill_price",
            optional_positive(self.average_fill_price, "average_fill_price"),
        )
        object.__setattr__(
            self,
            "last_fill_quantity",
            nonnegative(self.last_fill_quantity, "last_fill_quantity"),
        )
        object.__setattr__(
            self,
            "last_fill_price",
            optional_positive(self.last_fill_price, "last_fill_price"),
        )
        if self.text is not None:
            object.__setattr__(self, "text", self.text.strip())


@dataclass(frozen=True, slots=True)
class Fill(Event):
    """One immutable execution fill."""

    fill_id: str
    order_id: str
    symbol: Symbol
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal = _ZERO
    liquidity: str | None = None
    event_type: ClassVar[EventType] = EventType.FILL

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "fill_id", text(self.fill_id, "fill_id"))
        object.__setattr__(self, "order_id", text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(
            self, "quantity", decimal(self.quantity, "quantity", positive=True)
        )
        object.__setattr__(self, "price", decimal(self.price, "price", positive=True))
        # Negative fees represent liquidity rebates and are valid accounting.
        object.__setattr__(self, "fee", exact_decimal(self.fee, "fee"))
        if self.liquidity is not None:
            object.__setattr__(self, "liquidity", text(self.liquidity, "liquidity"))


@dataclass(frozen=True, slots=True)
class Position(Event):
    """An account's signed position and valuation at an explicit timestamp."""

    account_id: str
    symbol: Symbol
    quantity: Decimal
    average_price: Decimal
    realized_pnl: Decimal = _ZERO
    market_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    event_type: ClassVar[EventType] = EventType.POSITION

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "account_id", text(self.account_id, "account_id"))
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, "quantity"))
        object.__setattr__(
            self, "average_price", nonnegative(self.average_price, "average_price")
        )
        object.__setattr__(
            self, "realized_pnl", exact_decimal(self.realized_pnl, "realized_pnl")
        )
        object.__setattr__(
            self, "market_price", optional_positive(self.market_price, "market_price")
        )
        object.__setattr__(
            self,
            "unrealized_pnl",
            optional_decimal(self.unrealized_pnl, "unrealized_pnl"),
        )

    @property
    def average_cost(self) -> Decimal:
        """Compatibility alias for average entry price."""
        return exact_decimal(self.average_price, "average_price")


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One price level in a deterministic order book."""

    price: Decimal
    quantity: Decimal
    order_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal(self.price, "price", positive=True))
        object.__setattr__(
            self, "quantity", decimal(self.quantity, "quantity", positive=True)
        )
        if (
            isinstance(self.order_count, bool)
            or not isinstance(self.order_count, int)
            or self.order_count < 0
        ):
            raise ValueError("order_count must not be negative")


@dataclass(frozen=True, slots=True)
class Book(Event):
    """A full deterministic top-of-book or depth snapshot."""

    symbol: Symbol
    bids: tuple[BookLevel, ...] | Sequence[BookLevel] = ()
    asks: tuple[BookLevel, ...] | Sequence[BookLevel] = ()
    venue: str | None = None
    event_type: ClassVar[EventType] = EventType.BOOK

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        bids = tuple(self.bids)
        asks = tuple(self.asks)
        if not all(isinstance(level, BookLevel) for level in (*bids, *asks)):
            raise TypeError("bids and asks must contain BookLevel values")
        if any(
            exact_decimal(left.price) < exact_decimal(right.price)
            for left, right in pairwise(bids)
        ):
            raise ValueError("bid levels must be sorted from high to low")
        if any(
            exact_decimal(left.price) > exact_decimal(right.price)
            for left, right in pairwise(asks)
        ):
            raise ValueError("ask levels must be sorted from low to high")
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)
        if self.venue is not None:
            object.__setattr__(self, "venue", text(self.venue, "venue"))


@dataclass(frozen=True, slots=True)
class AccountSnapshot(Event):
    """A complete account valuation snapshot."""

    account_id: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    margin_used: Decimal = _ZERO
    positions: tuple[Position, ...] | Sequence[Position] = ()
    event_type: ClassVar[EventType] = EventType.ACCOUNT

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "account_id", text(self.account_id, "account_id"))
        object.__setattr__(self, "cash", exact_decimal(self.cash, "cash"))
        object.__setattr__(self, "equity", exact_decimal(self.equity, "equity"))
        object.__setattr__(
            self, "buying_power", exact_decimal(self.buying_power, "buying_power")
        )
        object.__setattr__(
            self, "margin_used", nonnegative(self.margin_used, "margin_used")
        )
        positions = tuple(self.positions)
        if not all(isinstance(position, Position) for position in positions):
            raise TypeError("positions must contain Position values")
        object.__setattr__(self, "positions", positions)


__all__ = [
    "AccountSnapshot",
    "Book",
    "BookLevel",
    "ExecutionReport",
    "Fill",
    "Position",
]
