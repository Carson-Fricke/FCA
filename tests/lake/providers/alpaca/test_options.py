import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from lake.catalog import ContractRecord, OptionType
from lake.options import AlpacaOptionsClient
from lake.providers.alpaca import (
    AlpacaProtocolError,
    NormalizedOptionQuote,
    NormalizedOptionTrade,
)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        **headers: str,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers: Mapping[str, str] = headers

    def json(self) -> object:
        return self.payload


class FakeTransport:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
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
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def _contract(
    symbol: str,
    underlying: str,
    expiration_date: date,
    strike_price: str,
    option_type: OptionType,
) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying=underlying,
        expiration_date=expiration_date,
        strike_price=Decimal(strike_price),
        option_type=option_type,
    )


SPY_CALL = _contract(
    "SPY240119C00450000", "SPY", date(2024, 1, 19), "450", OptionType.CALL
)
QQQ_PUT = _contract(
    "QQQ240126P00350000", "QQQ", date(2024, 1, 26), "350", OptionType.PUT
)


def _trade(timestamp: str, price: str, condition: str = "I") -> dict[str, object]:
    return {"t": timestamp, "x": "B", "p": price, "s": 2, "c": condition}


def _quote(timestamp: str) -> dict[str, object]:
    return {
        "t": timestamp,
        "bx": "W",
        "bp": "1.20",
        "bs": 10,
        "ax": "X",
        "ap": "1.30",
        "as": 20,
        "c": "A",
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_opra_trades_paginate_across_selected_symbols_and_preserve_contract_metadata() -> (
    None
):
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "trades": {
                        SPY_CALL.symbol: [_trade("2024-01-19T15:00:00Z", "2.25")]
                    },
                    "next_page_token": "next",
                }
            ),
            FakeResponse(
                {
                    "trades": {
                        QQQ_PUT.symbol: [_trade("2024-01-19T15:01:00Z", "3.50", "E")]
                    },
                    "next_page_token": None,
                }
            ),
        ]
    )

    async def exercise() -> tuple[NormalizedOptionTrade, ...]:
        async with AlpacaOptionsClient(transport=transport) as client:
            return await client.get_option_trades(
                (SPY_CALL, QQQ_PUT),
                "2024-01-19T00:00:00Z",
                "2024-01-20T00:00:00Z",
                limit=2,
            )

    trades = _run(exercise())

    assert trades == (
        NormalizedOptionTrade(
            timestamp=datetime(2024, 1, 19, 15, tzinfo=UTC),
            symbol=SPY_CALL.symbol,
            underlying="SPY",
            expiration_date=date(2024, 1, 19),
            strike_price=Decimal("450"),
            option_type=OptionType.CALL,
            exchange="B",
            price=Decimal("2.25"),
            size=2,
            condition="I",
        ),
        NormalizedOptionTrade(
            timestamp=datetime(2024, 1, 19, 15, 1, tzinfo=UTC),
            symbol=QQQ_PUT.symbol,
            underlying="QQQ",
            expiration_date=date(2024, 1, 26),
            strike_price=Decimal("350"),
            option_type=OptionType.PUT,
            exchange="B",
            price=Decimal("3.50"),
            size=2,
            condition="E",
        ),
    )
    assert trades[0].osi == SPY_CALL.symbol
    assert trades[0].expiry == date(2024, 1, 19)
    assert trades[1].right is OptionType.PUT
    assert transport.calls[0]["url"].endswith("/v1beta1/options/trades")
    assert transport.calls[0]["params"] == {
        "symbols": f"{SPY_CALL.symbol},{QQQ_PUT.symbol}",
        "start": "2024-01-19T00:00:00Z",
        "end": "2024-01-20T00:00:00Z",
        "limit": 2,
        "sort": "asc",
    }
    assert transport.calls[1]["params"]["page_token"] == "next"


def test_opra_quotes_reuse_retry_policy_and_normalize_quote_fields() -> None:
    transport = FakeTransport(
        [
            FakeResponse({}, status_code=429, **{"Retry-After": "0"}),
            FakeResponse(
                {"quotes": {QQQ_PUT.symbol: [_quote("2024-01-19T15:00:00.123456Z")]}}
            ),
        ]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def exercise() -> tuple[NormalizedOptionQuote, ...]:
        async with AlpacaOptionsClient(
            transport=transport,
            max_attempts=2,
            sleep=sleep,
        ) as client:
            return await client.fetch_option_quotes([QQQ_PUT], "start", "end")

    quotes = _run(exercise())

    assert quotes == (
        NormalizedOptionQuote(
            timestamp=datetime(2024, 1, 19, 15, 0, 0, 123456, tzinfo=UTC),
            symbol=QQQ_PUT.symbol,
            underlying="QQQ",
            expiration_date=date(2024, 1, 26),
            strike_price=Decimal("350"),
            option_type=OptionType.PUT,
            bid_exchange="W",
            bid_price=Decimal("1.20"),
            bid_size=10,
            ask_exchange="X",
            ask_price=Decimal("1.30"),
            ask_size=20,
            condition="A",
        ),
    )
    assert sleeps == [0.0]
    assert len(transport.calls) == 2
    assert transport.calls[0]["params"] == transport.calls[1]["params"]


def test_option_requests_batch_selected_contracts_at_provider_limit() -> None:
    contracts = tuple(
        _contract(
            f"SPY240119C{strike:08d}",
            "SPY",
            date(2024, 1, 19),
            str(strike / 100),
            OptionType.CALL,
        )
        for strike in range(10000, 10101)
    )
    transport = FakeTransport(
        [FakeResponse({"trades": {}, "next_page_token": None}) for _ in range(2)]
    )

    async def exercise() -> tuple[NormalizedOptionTrade, ...]:
        async with AlpacaOptionsClient(transport=transport) as client:
            return await client.fetch_option_trades(contracts, "start", "end")

    assert _run(exercise()) == ()
    assert len(transport.calls) == 2
    assert len(transport.calls[0]["params"]["symbols"].split(",")) == 100
    assert len(transport.calls[1]["params"]["symbols"].split(",")) == 1


def test_option_response_rejects_symbol_outside_selected_front_set() -> None:
    transport = FakeTransport(
        [
            FakeResponse(
                {"quotes": {"SPY240119C00450000": [_quote("2024-01-19T15:00:00Z")]}}
            )
        ]
    )

    async def exercise() -> None:
        async with AlpacaOptionsClient(transport=transport) as client:
            await client.fetch_option_quotes([QQQ_PUT], "start", "end")

    with pytest.raises(AlpacaProtocolError, match="unselected symbol"):
        _run(exercise())
