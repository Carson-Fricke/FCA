import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from lake.configuration import load_universes
from lake.contracts import AssetClass, DataType, schema_for
from lake.crypto import AlpacaCryptoClient
from lake.providers.alpaca import (
    NormalizedCryptoQuote,
    NormalizedCryptoTrade,
)
from lake.storage import normalize_records

ROOT = Path(__file__).parents[4]


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200
        self.headers: Mapping[str, str] = {}

    def json(self) -> object:
        return self.payload


class CryptoTransport:
    def __init__(
        self,
        responses: Mapping[tuple[str, str, str | None], object],
    ) -> None:
        self.responses = dict(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        timeout: float,
    ) -> FakeResponse:
        del headers, timeout
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params),
            }
        )
        page_token = params.get("page_token")
        if page_token is not None and not isinstance(page_token, str):
            raise AssertionError("page_token must be a string")
        symbols = params.get("symbols")
        if not isinstance(symbols, str):
            raise AssertionError("crypto request must include symbols")
        key = (url.rsplit("/", maxsplit=1)[-1], symbols, page_token)
        return FakeResponse(self.responses[key])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _trade(
    timestamp: str,
    *,
    trade_id: int | None = None,
    exchange: str | None = None,
    taker_side: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "t": timestamp,
        "p": "41234.123456789012345678",
        "s": "0.000000010000000000",
    }
    if trade_id is not None:
        row["i"] = trade_id
    if exchange is not None:
        row["x"] = exchange
    if taker_side is not None:
        row["tks"] = taker_side
    return row


def _quote(timestamp: str) -> dict[str, object]:
    return {
        "t": timestamp,
        "bp": "41234.12",
        "bs": "0.100000000000000000",
        "ap": "41235.12",
        "as": "0.200000000000000000",
    }


def test_configured_crypto_trades_paginate_and_map_to_t021_schema() -> None:
    pair = load_universes(ROOT).crypto.pairs[0]
    transport = CryptoTransport(
        {
            ("trades", pair, None): {
                "trades": {pair: [_trade("2024-01-19T14:30:00Z")]},
                "next_page_token": "page-2",
            },
            ("trades", pair, "page-2"): {
                "trades": {
                    pair: [
                        _trade(
                            "2024-01-19T14:31:00.123456789Z",
                            trade_id=7,
                            exchange="CBSE",
                            taker_side="B",
                        )
                    ]
                },
                "next_page_token": None,
            },
        }
    )

    async def exercise() -> tuple[NormalizedCryptoTrade, ...]:
        async with AlpacaCryptoClient(transport=transport) as client:
            return await client.fetch_crypto_trades(
                [" btc/usd ", pair],
                date(2024, 1, 19),
                date(2024, 1, 20),
                limit=100,
            )

    trades = _run(exercise())

    assert trades == (
        NormalizedCryptoTrade(
            timestamp=datetime(2024, 1, 19, 14, 30, tzinfo=UTC),
            symbol="BTC/USD",
            trade_id=None,
            exchange=None,
            price=Decimal("41234.123456789012345678"),
            size=Decimal("0.000000010000000000"),
            taker_side=None,
        ),
        NormalizedCryptoTrade(
            timestamp=datetime(2024, 1, 19, 14, 31, 0, 123456, tzinfo=UTC),
            symbol="BTC/USD",
            trade_id=7,
            exchange="CBSE",
            price=Decimal("41234.123456789012345678"),
            size=Decimal("0.000000010000000000"),
            taker_side="B",
        ),
    )
    assert [call["url"] for call in transport.calls] == [
        "https://data.alpaca.markets/v1beta3/crypto/us/trades",
        "https://data.alpaca.markets/v1beta3/crypto/us/trades",
    ]
    assert transport.calls[0]["params"] == {
        "start": "2024-01-19T00:00:00Z",
        "end": "2024-01-20T00:00:00Z",
        "limit": 100,
        "sort": "asc",
        "symbols": "BTC/USD",
    }
    assert transport.calls[1]["params"]["page_token"] == "page-2"

    table = normalize_records(
        [
            {
                "timestamp": trade.timestamp,
                "symbol": trade.symbol,
                "trade_id": trade.trade_id,
                "exchange": trade.exchange,
                "price": trade.price,
                "size": trade.size,
                "taker_side": trade.taker_side,
            }
            for trade in trades
        ],
        asset_class=AssetClass.CRYPTO,
        data_type=DataType.TRADE,
        symbol=pair,
    )
    assert table.schema.equals(
        schema_for(AssetClass.CRYPTO, DataType.TRADE), check_metadata=True
    )
    assert table.column("size")[0].as_py() == Decimal("0.000000010000000000")


def test_configured_crypto_quotes_keep_feed_specific_fields_nullable() -> None:
    pairs = load_universes(ROOT).crypto.pairs
    transport = CryptoTransport(
        {
            ("quotes", pairs[0], None): {
                "quotes": {pairs[0]: [_quote("2024-01-19T14:30:00Z")]},
                "next_page_token": None,
            },
            ("quotes", pairs[1], None): {
                "quotes": {pairs[1]: [_quote("2024-01-19T14:31:00Z")]},
                "next_page_token": None,
            },
        }
    )

    async def exercise() -> tuple[NormalizedCryptoQuote, ...]:
        async with AlpacaCryptoClient(transport=transport) as client:
            return await client.fetch_crypto_quotes(pairs, "start", "end")

    quotes = _run(exercise())

    assert quotes == (
        NormalizedCryptoQuote(
            timestamp=datetime(2024, 1, 19, 14, 30, tzinfo=UTC),
            symbol="BTC/USD",
            bid_exchange=None,
            bid_price=Decimal("41234.12"),
            bid_size=Decimal("0.100000000000000000"),
            ask_exchange=None,
            ask_price=Decimal("41235.12"),
            ask_size=Decimal("0.200000000000000000"),
        ),
        NormalizedCryptoQuote(
            timestamp=datetime(2024, 1, 19, 14, 31, tzinfo=UTC),
            symbol="ETH/USD",
            bid_exchange=None,
            bid_price=Decimal("41234.12"),
            bid_size=Decimal("0.100000000000000000"),
            ask_exchange=None,
            ask_price=Decimal("41235.12"),
            ask_size=Decimal("0.200000000000000000"),
        ),
    )
    for pair, quote in zip(pairs, quotes, strict=True):
        table = normalize_records(
            [
                {
                    "timestamp": quote.timestamp,
                    "symbol": quote.symbol,
                    "bid_exchange": quote.bid_exchange,
                    "bid_price": quote.bid_price,
                    "bid_size": quote.bid_size,
                    "ask_exchange": quote.ask_exchange,
                    "ask_price": quote.ask_price,
                    "ask_size": quote.ask_size,
                }
            ],
            asset_class=AssetClass.CRYPTO,
            data_type=DataType.QUOTE,
            symbol=pair,
        )
        assert table.schema.equals(
            schema_for(AssetClass.CRYPTO, DataType.QUOTE), check_metadata=True
        )
    assert table.column("bid_exchange")[0].as_py() is None
    assert isinstance(quotes[0], NormalizedCryptoQuote)
    assert pa.types.is_decimal(table.column("bid_size").type)
