from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.core.types import (
    AccountSnapshot,
    Book,
    BookLevel,
    ExecutionReport,
    ExecutionType,
    Fill,
    MultiLegOrder,
    OrderLeg,
    OrderStatus,
    OrderType,
    ParquetCodec,
    Position,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
)

STAMP = datetime(2024, 1, 19, 14, 30, 0, 123456, tzinfo=UTC)


def test_domain_records_reject_float_and_are_frozen() -> None:
    quote = QuoteEvent(
        "q1",
        STAMP,
        1,
        Symbol(" aapl "),
        Decimal("190.1200"),
        Decimal("10.00"),
        Decimal("190.1300"),
        Decimal("20"),
    )

    assert quote.symbol == "AAPL"
    assert quote.bid_price.as_tuple().exponent == -4
    with pytest.raises(TypeError):
        QuoteEvent(
            "q2",
            STAMP,
            2,
            Symbol("AAPL"),
            cast(Decimal, 190.12),
            Decimal("10"),
            Decimal("190.13"),
            Decimal("20"),
        )
    with pytest.raises((AttributeError, TypeError)):
        quote.symbol = Symbol("MSFT")  # ty: ignore[invalid-assignment]


def test_single_and_multi_leg_orders_validate_semantics() -> None:
    order = SingleLegOrder(
        "e1",
        STAMP,
        3,
        "o1",
        Symbol("AAPL"),
        Side.BUY,
        Decimal("2.000"),
        OrderType.LIMIT,
        TimeInForce.GTC,
        Decimal("190.10"),
    )
    multi = MultiLegOrder(
        "e2",
        STAMP,
        4,
        "o2",
        (
            OrderLeg(Symbol("AAPL"), Side.BUY, Decimal("1")),
            OrderLeg(Symbol("MSFT"), Side.SELL, Decimal("2")),
        ),
    )

    assert order.limit_price == Decimal("190.10")
    assert len(multi.legs) == 2
    with pytest.raises(ValueError):
        SingleLegOrder(
            "e-bad",
            STAMP,
            0,
            "bad",
            Symbol("AAPL"),
            Side.BUY,
            Decimal("1"),
            OrderType.LIMIT,
        )
    with pytest.raises(ValueError, match="invalid"):
        SingleLegOrder(
            "e-bad-price",
            STAMP,
            1,
            "bad-price",
            Symbol("AAPL"),
            Side.BUY,
            Decimal("1"),
            OrderType.MARKET,
            limit_price=Decimal("1.00"),
        )


def test_all_core_records_round_trip_with_decimal_scale() -> None:
    records = [
        QuoteEvent(
            "q1",
            STAMP,
            1,
            Symbol("AAPL"),
            Decimal("190.1200"),
            Decimal("10.00"),
            Decimal("190.1300"),
            Decimal("20.000"),
        ),
        TradeEvent(
            "t1", STAMP, 2, Symbol("AAPL"), Decimal("190.125000"), Decimal("0.0100")
        ),
        SingleLegOrder(
            "e3",
            STAMP,
            3,
            "o1",
            Symbol("AAPL"),
            Side.BUY,
            Decimal("2.000"),
            OrderType.LIMIT,
            TimeInForce.DAY,
            Decimal("190.10"),
        ),
        MultiLegOrder(
            "e4",
            STAMP,
            4,
            "o2",
            (
                OrderLeg(Symbol("AAPL"), Side.BUY, Decimal("1.00")),
                OrderLeg(Symbol("MSFT"), Side.SELL, Decimal("2")),
            ),
        ),
        ExecutionReport(
            "r1",
            STAMP,
            5,
            "report",
            "o1",
            OrderStatus.PARTIALLY_FILLED,
            ExecutionType.PARTIAL_FILL,
            Decimal("2.000"),
            Decimal("1.00"),
            Decimal("1.000"),
            Decimal("190.1200"),
            Decimal("1.00"),
            Decimal("190.1200"),
        ),
        Fill(
            "f1",
            STAMP,
            6,
            "fill",
            "o1",
            Symbol("AAPL"),
            Side.BUY,
            Decimal("1.00"),
            Decimal("190.1200"),
            Decimal("0.0100"),
        ),
        Position(
            "p1",
            STAMP,
            7,
            "acct",
            Symbol("AAPL"),
            Decimal("1.00"),
            Decimal("190.1200"),
            Decimal("0.00"),
            Decimal("190.1300"),
            Decimal("0.0100"),
        ),
        Book(
            "b1",
            STAMP,
            8,
            Symbol("AAPL"),
            (BookLevel(Decimal("190.12"), Decimal("10.00")),),
            (BookLevel(Decimal("190.13"), Decimal("20.00")),),
        ),
        AccountSnapshot(
            "a1",
            STAMP,
            9,
            "acct",
            Decimal("100.00"),
            Decimal("110.00"),
            Decimal("200.00"),
            positions=(),
        ),
    ]

    for record in records:
        restored = ParquetCodec.decode(ParquetCodec.encode([record]), type(record))[0]
        assert restored == record

    limited_spread = MultiLegOrder(
        "e-limit",
        STAMP,
        10,
        "spread",
        (
            OrderLeg(Symbol("AAPL"), Side.BUY, Decimal("1")),
            OrderLeg(Symbol("MSFT"), Side.SELL, Decimal("1")),
        ),
        OrderType.LIMIT,
        TimeInForce.DAY,
        limit_price=Decimal("1.25"),
    )
    assert (
        ParquetCodec.decode(ParquetCodec.encode([limited_spread]), MultiLegOrder)[0]
        == limited_spread
    )


def test_execution_report_ledger_is_exact() -> None:
    with pytest.raises(ValueError, match="must equal"):
        ExecutionReport(
            "r1",
            STAMP,
            1,
            "report",
            "o1",
            OrderStatus.FILLED,
            ExecutionType.FILL,
            Decimal("1"),
            Decimal("0.5"),
            Decimal("0.4"),
        )
