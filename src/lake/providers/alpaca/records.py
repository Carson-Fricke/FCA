"""Normalized provider records independent of HTTP transport."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from lake.catalog.models import OptionType


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    timestamp: datetime
    symbol: str
    trade_id: int
    exchange: str
    price: Decimal
    size: int
    conditions: tuple[str, ...]
    tape: str
    update: str | None


@dataclass(frozen=True, slots=True)
class NormalizedQuote:
    timestamp: datetime
    symbol: str
    bid_exchange: str
    bid_price: Decimal
    bid_size: int
    ask_exchange: str
    ask_price: Decimal
    ask_size: int
    conditions: tuple[str, ...]
    tape: str


@dataclass(frozen=True, slots=True)
class NormalizedOptionTrade:
    timestamp: datetime
    symbol: str
    underlying: str
    expiration_date: date
    strike_price: Decimal
    option_type: OptionType
    exchange: str
    price: Decimal
    size: int
    condition: str

    @property
    def osi(self) -> str:
        return self.symbol

    @property
    def expiry(self) -> date:
        return self.expiration_date

    @property
    def strike(self) -> Decimal:
        return self.strike_price

    @property
    def right(self) -> OptionType:
        return self.option_type

    @property
    def call_put(self) -> OptionType:
        return self.option_type


@dataclass(frozen=True, slots=True)
class NormalizedOptionQuote:
    timestamp: datetime
    symbol: str
    underlying: str
    expiration_date: date
    strike_price: Decimal
    option_type: OptionType
    bid_exchange: str
    bid_price: Decimal
    bid_size: int
    ask_exchange: str
    ask_price: Decimal
    ask_size: int
    condition: str

    @property
    def osi(self) -> str:
        return self.symbol

    @property
    def expiry(self) -> date:
        return self.expiration_date

    @property
    def strike(self) -> Decimal:
        return self.strike_price

    @property
    def right(self) -> OptionType:
        return self.option_type

    @property
    def call_put(self) -> OptionType:
        return self.option_type


@dataclass(frozen=True, slots=True)
class NormalizedCryptoTrade:
    timestamp: datetime
    symbol: str
    trade_id: int | None
    exchange: str | None
    price: Decimal
    size: Decimal
    taker_side: str | None


@dataclass(frozen=True, slots=True)
class NormalizedCryptoQuote:
    timestamp: datetime
    symbol: str
    bid_exchange: str | None
    bid_price: Decimal
    bid_size: Decimal
    ask_exchange: str | None
    ask_price: Decimal
    ask_size: Decimal


Trade = NormalizedTrade
Quote = NormalizedQuote
OptionTrade = NormalizedOptionTrade
OptionQuote = NormalizedOptionQuote
CryptoTrade = NormalizedCryptoTrade
CryptoQuote = NormalizedCryptoQuote

__all__ = [
    "CryptoQuote",
    "CryptoTrade",
    "NormalizedCryptoQuote",
    "NormalizedCryptoTrade",
    "NormalizedOptionQuote",
    "NormalizedOptionTrade",
    "NormalizedQuote",
    "NormalizedTrade",
    "OptionQuote",
    "OptionTrade",
    "Quote",
    "Trade",
]
