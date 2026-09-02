"""Alpaca crypto market-data client facade."""

from lake.providers.alpaca import (
    AlpacaClient,
    NormalizedCryptoQuote,
    NormalizedCryptoTrade,
)


class AlpacaCryptoClient(AlpacaClient):
    """Fetch historical trades and quotes for configured crypto pairs."""


CryptoClient = AlpacaCryptoClient
CryptoTrade = NormalizedCryptoTrade
CryptoQuote = NormalizedCryptoQuote


__all__ = [
    "AlpacaCryptoClient",
    "CryptoClient",
    "CryptoQuote",
    "CryptoTrade",
    "NormalizedCryptoQuote",
    "NormalizedCryptoTrade",
]
