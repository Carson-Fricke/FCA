"""Immutable market-data event records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import decimal, sequence, text, timestamp


class EventType(StrEnum):
    QUOTE = "quote"
    TRADE = "trade"
    ORDER = "order"
    MULTI_LEG_ORDER = "multi_leg_order"
    EXECUTION_REPORT = "execution_report"
    FILL = "fill"
    POSITION = "position"
    BOOK = "book"
    ACCOUNT = "account"


@dataclass(frozen=True, slots=True)
class Event:
    """Common identity and ordering fields for all event records."""

    event_id: str
    timestamp: datetime
    sequence: int
    event_type: ClassVar[EventType] = EventType.ORDER

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", text(self.event_id, "event_id"))
        object.__setattr__(self, "timestamp", timestamp(self.timestamp))
        object.__setattr__(self, "sequence", sequence(self.sequence))


@dataclass(frozen=True, slots=True)
class QuoteEvent(Event):
    """A top-of-book quote with exact price and quantity values."""

    symbol: Symbol
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    venue: str | None = None
    event_type: ClassVar[EventType] = EventType.QUOTE

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(
            self, "bid_price", decimal(self.bid_price, "bid_price", positive=True)
        )
        object.__setattr__(
            self,
            "bid_quantity",
            decimal(self.bid_quantity, "bid_quantity", positive=True),
        )
        object.__setattr__(
            self, "ask_price", decimal(self.ask_price, "ask_price", positive=True)
        )
        object.__setattr__(
            self,
            "ask_quantity",
            decimal(self.ask_quantity, "ask_quantity", positive=True),
        )
        if self.venue is not None:
            object.__setattr__(self, "venue", text(self.venue, "venue"))


@dataclass(frozen=True, slots=True)
class TradeEvent(Event):
    """A market trade with exact price and quantity values."""

    symbol: Symbol
    price: Decimal
    quantity: Decimal
    venue: str | None = None
    trade_id: str | None = None
    event_type: ClassVar[EventType] = EventType.TRADE

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "price", decimal(self.price, "price", positive=True))
        object.__setattr__(
            self, "quantity", decimal(self.quantity, "quantity", positive=True)
        )
        if self.venue is not None:
            object.__setattr__(self, "venue", text(self.venue, "venue"))
        if self.trade_id is not None:
            object.__setattr__(self, "trade_id", text(self.trade_id, "trade_id"))


__all__ = ["Event", "EventType", "QuoteEvent", "TradeEvent"]
