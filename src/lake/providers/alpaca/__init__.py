"""Alpaca provider facade."""

from lake.providers.alpaca.client import (
    AlpacaClient,
    AlpacaHTTPError,
    AlpacaProtocolError,
    AsyncResponse,
    AsyncTransport,
)
from lake.providers.alpaca.records import (
    CryptoQuote,
    CryptoTrade,
    NormalizedCryptoQuote,
    NormalizedCryptoTrade,
    NormalizedOptionQuote,
    NormalizedOptionTrade,
    NormalizedQuote,
    NormalizedTrade,
    OptionQuote,
    OptionTrade,
    Quote,
    Trade,
)

__all__ = [
    "AlpacaClient",
    "AlpacaHTTPError",
    "AlpacaProtocolError",
    "AsyncResponse",
    "AsyncTransport",
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
