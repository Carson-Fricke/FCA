"""Versioned PyArrow schemas for persisted market trades and quotes.

Fields normalize Alpaca SIP, OPRA, and crypto payload names while preserving
their native precision and identifiers. Increment ``SCHEMA_VERSION`` for any
field, type, order, nullability, or semantic change.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

import pyarrow as pa

from lake.partition_paths import AssetClass, Dataset, DataType

SCHEMA_VERSION: Final = "1"

_TIMESTAMP: Final = pa.timestamp("ns", tz="UTC")
_PRICE: Final = pa.decimal128(38, 18)
_QUANTITY: Final = pa.decimal128(38, 18)
_CONDITIONS: Final = pa.list_(pa.field("item", pa.string(), nullable=False))


def _field(name: str, data_type: pa.DataType, *, nullable: bool = False) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


def _schema(name: str, fields: Sequence[pa.Field]) -> pa.Schema:
    return pa.schema(
        fields,
        metadata={
            b"lake.schema": name.encode("ascii"),
            b"lake.schema.version": SCHEMA_VERSION.encode("ascii"),
        },
    )


EQUITY_TRADE_SCHEMA: Final = _schema(
    "equity.trade",
    (
        _field("timestamp", _TIMESTAMP),
        _field("symbol", pa.string()),
        _field("trade_id", pa.uint64()),
        _field("exchange", pa.string()),
        _field("price", _PRICE),
        _field("size", pa.uint32()),
        _field("conditions", _CONDITIONS),
        _field("tape", pa.string()),
        _field("update", pa.string(), nullable=True),
    ),
)

EQUITY_QUOTE_SCHEMA: Final = _schema(
    "equity.quote",
    (
        _field("timestamp", _TIMESTAMP),
        _field("symbol", pa.string()),
        _field("bid_exchange", pa.string()),
        _field("bid_price", _PRICE),
        _field("bid_size", pa.uint32()),
        _field("ask_exchange", pa.string()),
        _field("ask_price", _PRICE),
        _field("ask_size", pa.uint32()),
        _field("conditions", _CONDITIONS),
        _field("tape", pa.string()),
    ),
)

OPTION_TRADE_SCHEMA: Final = _schema(
    "option.trade",
    (
        _field("timestamp", _TIMESTAMP),
        _field("symbol", pa.string()),
        _field("exchange", pa.string()),
        _field("price", _PRICE),
        _field("size", pa.uint32()),
        _field("condition", pa.string()),
    ),
)

OPTION_QUOTE_SCHEMA: Final = _schema(
    "option.quote",
    (
        _field("timestamp", _TIMESTAMP),
        _field("symbol", pa.string()),
        _field("bid_exchange", pa.string()),
        _field("bid_price", _PRICE),
        _field("bid_size", pa.uint32()),
        _field("ask_exchange", pa.string()),
        _field("ask_price", _PRICE),
        _field("ask_size", pa.uint32()),
        _field("condition", pa.string()),
    ),
)

CRYPTO_TRADE_SCHEMA: Final = _schema(
    "crypto.trade",
    (
        _field("timestamp", _TIMESTAMP),
        _field("symbol", pa.string()),
        _field("trade_id", pa.int64(), nullable=True),
        _field("exchange", pa.string(), nullable=True),
        _field("price", _PRICE),
        _field("size", _QUANTITY),
        _field("taker_side", pa.string(), nullable=True),
    ),
)

CRYPTO_QUOTE_SCHEMA: Final = _schema(
    "crypto.quote",
    (
        _field("timestamp", _TIMESTAMP),
        _field("symbol", pa.string()),
        _field("bid_exchange", pa.string(), nullable=True),
        _field("bid_price", _PRICE),
        _field("bid_size", _QUANTITY),
        _field("ask_exchange", pa.string(), nullable=True),
        _field("ask_price", _PRICE),
        _field("ask_size", _QUANTITY),
    ),
)

SCHEMAS: Final[Mapping[Dataset, pa.Schema]] = MappingProxyType(
    {
        (AssetClass.EQUITY, DataType.TRADE): EQUITY_TRADE_SCHEMA,
        (AssetClass.EQUITY, DataType.QUOTE): EQUITY_QUOTE_SCHEMA,
        (AssetClass.OPTION, DataType.TRADE): OPTION_TRADE_SCHEMA,
        (AssetClass.OPTION, DataType.QUOTE): OPTION_QUOTE_SCHEMA,
        (AssetClass.CRYPTO, DataType.TRADE): CRYPTO_TRADE_SCHEMA,
        (AssetClass.CRYPTO, DataType.QUOTE): CRYPTO_QUOTE_SCHEMA,
    }
)


def schema_for(asset_class: AssetClass, data_type: DataType) -> pa.Schema:
    """Return the stable schema for a supported asset and data type."""
    return SCHEMAS[(asset_class, data_type)]
