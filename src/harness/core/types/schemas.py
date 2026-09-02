"""Versioned Arrow schemas for harness market and accounting records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import pyarrow as pa

from harness.core.types.accounting import (
    AccountSnapshot,
    Book,
    ExecutionReport,
    Fill,
    Position,
)
from harness.core.types.common import SCHEMA_VERSION
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.core.types.orders import MultiLegOrder, SingleLegOrder

_DECIMAL_METADATA: Final = b"harness.decimal.encoding"


def _field(name: str, data_type: pa.DataType, *, nullable: bool = False) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


def _schema(name: str, fields: Sequence[pa.Field]) -> pa.Schema:
    return pa.schema(
        fields,
        metadata={
            b"harness.schema": name.encode("ascii"),
            b"harness.schema.version": SCHEMA_VERSION.encode("ascii"),
            _DECIMAL_METADATA: b"string-exact",
        },
    )


_BASE_FIELDS = (
    _field("event_id", pa.string()),
    _field("timestamp", pa.timestamp("ns", tz="UTC")),
    _field("sequence", pa.uint64()),
)
_DECIMAL = pa.string()

QUOTE_SCHEMA: Final = _schema(
    "quote",
    (
        *_BASE_FIELDS,
        _field("symbol", pa.string()),
        _field("bid_price", _DECIMAL),
        _field("bid_quantity", _DECIMAL),
        _field("ask_price", _DECIMAL),
        _field("ask_quantity", _DECIMAL),
        _field("venue", pa.string(), nullable=True),
    ),
)
TRADE_SCHEMA: Final = _schema(
    "trade",
    (
        *_BASE_FIELDS,
        _field("symbol", pa.string()),
        _field("price", _DECIMAL),
        _field("quantity", _DECIMAL),
        _field("venue", pa.string(), nullable=True),
        _field("trade_id", pa.string(), nullable=True),
    ),
)
ORDER_SCHEMA: Final = _schema(
    "single_leg_order",
    (
        *_BASE_FIELDS,
        _field("order_id", pa.string()),
        _field("symbol", pa.string()),
        _field("side", pa.string()),
        _field("quantity", _DECIMAL),
        _field("order_type", pa.string()),
        _field("time_in_force", pa.string()),
        _field("limit_price", _DECIMAL, nullable=True),
        _field("stop_price", _DECIMAL, nullable=True),
        _field("client_order_id", pa.string(), nullable=True),
    ),
)
MULTI_LEG_ORDER_SCHEMA: Final = _schema(
    "multi_leg_order",
    (
        *_BASE_FIELDS,
        _field("order_id", pa.string()),
        _field("legs", pa.string()),
        _field("order_type", pa.string()),
        _field("time_in_force", pa.string()),
        _field("client_order_id", pa.string(), nullable=True),
        _field("limit_price", _DECIMAL, nullable=True),
    ),
)
REPORT_SCHEMA: Final = _schema(
    "execution_report",
    (
        *_BASE_FIELDS,
        _field("report_id", pa.string()),
        _field("order_id", pa.string()),
        _field("status", pa.string()),
        _field("execution_type", pa.string()),
        _field("order_quantity", _DECIMAL),
        _field("filled_quantity", _DECIMAL),
        _field("remaining_quantity", _DECIMAL),
        _field("average_fill_price", _DECIMAL, nullable=True),
        _field("last_fill_quantity", _DECIMAL),
        _field("last_fill_price", _DECIMAL, nullable=True),
        _field("text", pa.string(), nullable=True),
    ),
)
FILL_SCHEMA: Final = _schema(
    "fill",
    (
        *_BASE_FIELDS,
        _field("fill_id", pa.string()),
        _field("order_id", pa.string()),
        _field("symbol", pa.string()),
        _field("side", pa.string()),
        _field("quantity", _DECIMAL),
        _field("price", _DECIMAL),
        _field("fee", _DECIMAL),
        _field("liquidity", pa.string(), nullable=True),
    ),
)
POSITION_SCHEMA: Final = _schema(
    "position",
    (
        *_BASE_FIELDS,
        _field("account_id", pa.string()),
        _field("symbol", pa.string()),
        _field("quantity", _DECIMAL),
        _field("average_price", _DECIMAL),
        _field("realized_pnl", _DECIMAL),
        _field("market_price", _DECIMAL, nullable=True),
        _field("unrealized_pnl", _DECIMAL, nullable=True),
    ),
)
BOOK_SCHEMA: Final = _schema(
    "book",
    (
        *_BASE_FIELDS,
        _field("symbol", pa.string()),
        _field("bids", pa.string()),
        _field("asks", pa.string()),
        _field("venue", pa.string(), nullable=True),
    ),
)
ACCOUNT_SCHEMA: Final = _schema(
    "account_snapshot",
    (
        *_BASE_FIELDS,
        _field("account_id", pa.string()),
        _field("cash", _DECIMAL),
        _field("equity", _DECIMAL),
        _field("buying_power", _DECIMAL),
        _field("margin_used", _DECIMAL),
        _field("positions", pa.string()),
    ),
)

PARQUET_SCHEMAS: Final[Mapping[type[object], pa.Schema]] = {
    QuoteEvent: QUOTE_SCHEMA,
    TradeEvent: TRADE_SCHEMA,
    SingleLegOrder: ORDER_SCHEMA,
    MultiLegOrder: MULTI_LEG_ORDER_SCHEMA,
    ExecutionReport: REPORT_SCHEMA,
    Fill: FILL_SCHEMA,
    Position: POSITION_SCHEMA,
    Book: BOOK_SCHEMA,
    AccountSnapshot: ACCOUNT_SCHEMA,
}
EVENT_SCHEMAS: Final[Mapping[str, pa.Schema]] = {
    "quote": QUOTE_SCHEMA,
    "trade": TRADE_SCHEMA,
    "order": ORDER_SCHEMA,
    "multi_leg_order": MULTI_LEG_ORDER_SCHEMA,
    "execution_report": REPORT_SCHEMA,
    "fill": FILL_SCHEMA,
    "position": POSITION_SCHEMA,
    "book": BOOK_SCHEMA,
    "account": ACCOUNT_SCHEMA,
}
SCHEMAS = EVENT_SCHEMAS

SYMBOL_SCHEMA: Final = _schema(
    "symbol",
    (_field("symbol", pa.string()), _field("kind", pa.string())),
)


__all__ = [
    "ACCOUNT_SCHEMA",
    "BOOK_SCHEMA",
    "EVENT_SCHEMAS",
    "FILL_SCHEMA",
    "MULTI_LEG_ORDER_SCHEMA",
    "ORDER_SCHEMA",
    "PARQUET_SCHEMAS",
    "POSITION_SCHEMA",
    "REPORT_SCHEMA",
    "SCHEMAS",
    "SYMBOL_SCHEMA",
    "TRADE_SCHEMA",
    "QUOTE_SCHEMA",
]
