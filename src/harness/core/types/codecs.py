"""Deterministic Parquet codecs for harness records and symbols."""

from __future__ import annotations

import io
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq

from harness.core.symbols import OsiSymbol, Symbol, canonical_symbol
from harness.core.types.accounting import (
    AccountSnapshot,
    Book,
    BookLevel,
    ExecutionReport,
    Fill,
    Position,
)
from harness.core.types.common import decimal_text
from harness.core.types.events import Event, QuoteEvent, TradeEvent
from harness.core.types.orders import MultiLegOrder, OrderLeg, SingleLegOrder
from harness.core.types.schemas import PARQUET_SCHEMAS, SYMBOL_SCHEMA


def _level_rows(levels: Iterable[BookLevel]) -> str:
    return json.dumps(
        [
            {
                "price": str(level.price),
                "quantity": str(level.quantity),
                "order_count": level.order_count,
            }
            for level in levels
        ],
        separators=(",", ":"),
    )


def _position_rows(positions: Iterable[Position]) -> str:
    return json.dumps(
        [
            {
                "event_id": position.event_id,
                "timestamp": position.timestamp.isoformat(),
                "sequence": position.sequence,
                "account_id": position.account_id,
                "symbol": str(position.symbol),
                "quantity": str(position.quantity),
                "average_price": str(position.average_price),
                "realized_pnl": str(position.realized_pnl),
                "market_price": decimal_text(position.market_price),
                "unrealized_pnl": decimal_text(position.unrealized_pnl),
            }
            for position in positions
        ],
        separators=(",", ":"),
    )


def _leg_rows(legs: Iterable[OrderLeg]) -> str:
    return json.dumps(
        [
            {
                "symbol": str(leg.symbol),
                "side": leg.side.value,
                "quantity": str(leg.quantity),
                "ratio": str(leg.ratio),
                "leg_id": leg.leg_id,
            }
            for leg in legs
        ],
        separators=(",", ":"),
    )


def _row(record: object) -> dict[str, object]:
    if not isinstance(record, Event):
        raise TypeError(f"unsupported Parquet record type: {type(record).__name__}")
    common = {
        "event_id": record.event_id,
        "timestamp": record.timestamp,
        "sequence": record.sequence,
    }
    if isinstance(record, QuoteEvent):
        return {
            **common,
            "symbol": str(record.symbol),
            "bid_price": str(record.bid_price),
            "bid_quantity": str(record.bid_quantity),
            "ask_price": str(record.ask_price),
            "ask_quantity": str(record.ask_quantity),
            "venue": record.venue,
        }
    if isinstance(record, TradeEvent):
        return {
            **common,
            "symbol": str(record.symbol),
            "price": str(record.price),
            "quantity": str(record.quantity),
            "venue": record.venue,
            "trade_id": record.trade_id,
        }
    if isinstance(record, SingleLegOrder):
        return {
            **common,
            "order_id": record.order_id,
            "symbol": str(record.symbol),
            "side": record.side.value,
            "quantity": str(record.quantity),
            "order_type": record.order_type.value,
            "time_in_force": record.time_in_force.value,
            "limit_price": decimal_text(record.limit_price),
            "stop_price": decimal_text(record.stop_price),
            "client_order_id": record.client_order_id,
        }
    if isinstance(record, MultiLegOrder):
        return {
            **common,
            "order_id": record.order_id,
            "legs": _leg_rows(record.legs),
            "order_type": record.order_type.value,
            "time_in_force": record.time_in_force.value,
            "client_order_id": record.client_order_id,
            "limit_price": decimal_text(record.limit_price),
        }
    if isinstance(record, ExecutionReport):
        return {
            **common,
            "report_id": record.report_id,
            "order_id": record.order_id,
            "status": record.status.value,
            "execution_type": record.execution_type.value,
            "order_quantity": str(record.order_quantity),
            "filled_quantity": str(record.filled_quantity),
            "remaining_quantity": str(record.remaining_quantity),
            "average_fill_price": decimal_text(record.average_fill_price),
            "last_fill_quantity": str(record.last_fill_quantity),
            "last_fill_price": decimal_text(record.last_fill_price),
            "text": record.text,
        }
    if isinstance(record, Fill):
        return {
            **common,
            "fill_id": record.fill_id,
            "order_id": record.order_id,
            "symbol": str(record.symbol),
            "side": record.side.value,
            "quantity": str(record.quantity),
            "price": str(record.price),
            "fee": str(record.fee),
            "liquidity": record.liquidity,
        }
    if isinstance(record, Position):
        return {
            **common,
            "account_id": record.account_id,
            "symbol": str(record.symbol),
            "quantity": str(record.quantity),
            "average_price": str(record.average_price),
            "realized_pnl": str(record.realized_pnl),
            "market_price": decimal_text(record.market_price),
            "unrealized_pnl": decimal_text(record.unrealized_pnl),
        }
    if isinstance(record, Book):
        return {
            **common,
            "symbol": str(record.symbol),
            "bids": _level_rows(record.bids),
            "asks": _level_rows(record.asks),
            "venue": record.venue,
        }
    if isinstance(record, AccountSnapshot):
        return {
            **common,
            "account_id": record.account_id,
            "cash": str(record.cash),
            "equity": str(record.equity),
            "buying_power": str(record.buying_power),
            "margin_used": str(record.margin_used),
            "positions": _position_rows(record.positions),
        }
    raise TypeError(f"unsupported Parquet record type: {type(record).__name__}")


T = TypeVar("T")


class ParquetCodec:
    """Encode and decode one homogeneous immutable record collection."""

    @staticmethod
    def schema_for(record_type: type[object]) -> pa.Schema:
        try:
            return PARQUET_SCHEMAS[record_type]
        except KeyError as error:
            raise TypeError(
                f"unsupported Parquet record type: {record_type.__name__}"
            ) from error

    @classmethod
    def encode(
        cls, records: Iterable[object], record_type: type[object] | None = None
    ) -> bytes:
        values = list(records)
        if record_type is None:
            if not values:
                raise ValueError("record_type is required when encoding no records")
            record_type = type(values[0])
        schema = cls.schema_for(record_type)
        if any(type(value) is not record_type for value in values):
            raise TypeError("all Parquet records must have the same exact type")
        table = pa.Table.from_pylist([_row(value) for value in values], schema=schema)
        output = io.BytesIO()
        pq.write_table(table, output, compression="zstd", version="2.6")
        return output.getvalue()

    @classmethod
    def decode(cls, payload: bytes, record_type: type[T]) -> tuple[T, ...]:
        table = pq.read_table(io.BytesIO(payload))
        schema = cls.schema_for(record_type)
        if not table.schema.equals(schema, check_metadata=True):
            raise ValueError("Parquet schema does not match the requested record type")
        return tuple(_from_row(record_type, row) for row in table.to_pylist())

    @classmethod
    def write(
        cls,
        path: str | Path,
        records: Iterable[object],
        record_type: type[object] | None = None,
    ) -> None:
        Path(path).write_bytes(cls.encode(records, record_type))

    @classmethod
    def read(cls, path: str | Path, record_type: type[T]) -> tuple[T, ...]:
        return cls.decode(Path(path).read_bytes(), record_type)


EventParquetCodec = ParquetCodec


class SymbolParquetCodec:
    """Round-trip canonical equity and OSI symbols without losing their kind."""

    @staticmethod
    def encode(symbols: Iterable[Symbol | str]) -> bytes:
        values = tuple(canonical_symbol(value) for value in symbols)
        table = pa.Table.from_pylist(
            [
                {
                    "symbol": str(value),
                    "kind": "osi" if isinstance(value, OsiSymbol) else "market",
                }
                for value in values
            ],
            schema=SYMBOL_SCHEMA,
        )
        output = io.BytesIO()
        pq.write_table(table, output, compression="zstd", version="2.6")
        return output.getvalue()

    @staticmethod
    def decode(payload: bytes) -> tuple[Symbol, ...]:
        table = pq.read_table(io.BytesIO(payload))
        if not table.schema.equals(SYMBOL_SCHEMA, check_metadata=True):
            raise ValueError("Parquet schema does not match the symbol schema")
        return tuple(
            OsiSymbol(row["symbol"]) if row["kind"] == "osi" else Symbol(row["symbol"])
            for row in table.to_pylist()
        )


def symbols_to_parquet(symbols: Iterable[Symbol | str]) -> bytes:
    """Serialize canonical symbols to exact Parquet bytes."""
    return SymbolParquetCodec.encode(symbols)


def symbols_from_parquet(payload: bytes) -> tuple[Symbol, ...]:
    """Deserialize canonical symbols from Parquet bytes."""
    return SymbolParquetCodec.decode(payload)


def to_parquet(
    records: Iterable[object], record_type: type[object] | None = None
) -> bytes:
    """Serialize records to deterministic, zstd-compressed Parquet bytes."""
    return ParquetCodec.encode(records, record_type)


def from_parquet[T](payload: bytes, record_type: type[T]) -> tuple[T, ...]:
    """Deserialize exact Parquet bytes into immutable domain records."""
    return ParquetCodec.decode(payload, record_type)


def _from_row[T](record_type: type[T], row: Mapping[str, Any]) -> T:
    common = {
        "event_id": row["event_id"],
        "timestamp": row["timestamp"],
        "sequence": row["sequence"],
    }
    if record_type is QuoteEvent:
        return record_type(  # type: ignore[call-arg]
            **common,
            symbol=row["symbol"],
            bid_price=row["bid_price"],
            bid_quantity=row["bid_quantity"],
            ask_price=row["ask_price"],
            ask_quantity=row["ask_quantity"],
            venue=row["venue"],
        )
    if record_type is TradeEvent:
        return record_type(  # type: ignore[call-arg]
            **common,
            symbol=row["symbol"],
            price=row["price"],
            quantity=row["quantity"],
            venue=row["venue"],
            trade_id=row["trade_id"],
        )
    if record_type is SingleLegOrder:
        return record_type(  # type: ignore[call-arg]
            **common,
            order_id=row["order_id"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=row["quantity"],
            order_type=row["order_type"],
            time_in_force=row["time_in_force"],
            limit_price=row["limit_price"],
            stop_price=row["stop_price"],
            client_order_id=row["client_order_id"],
        )
    if record_type is MultiLegOrder:
        legs = tuple(OrderLeg(**leg) for leg in json.loads(row["legs"]))
        return record_type(  # type: ignore[call-arg]
            **common,
            order_id=row["order_id"],
            legs=legs,
            order_type=row["order_type"],
            time_in_force=row["time_in_force"],
            client_order_id=row["client_order_id"],
            limit_price=row["limit_price"],
        )
    if record_type is ExecutionReport:
        return record_type(  # type: ignore[call-arg]
            **common,
            report_id=row["report_id"],
            order_id=row["order_id"],
            status=row["status"],
            execution_type=row["execution_type"],
            order_quantity=row["order_quantity"],
            filled_quantity=row["filled_quantity"],
            remaining_quantity=row["remaining_quantity"],
            average_fill_price=row["average_fill_price"],
            last_fill_quantity=row["last_fill_quantity"],
            last_fill_price=row["last_fill_price"],
            text=row["text"],
        )
    if record_type is Fill:
        return record_type(  # type: ignore[call-arg]
            **common,
            fill_id=row["fill_id"],
            order_id=row["order_id"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=row["quantity"],
            price=row["price"],
            fee=row["fee"],
            liquidity=row["liquidity"],
        )
    if record_type is Position:
        return record_type(  # type: ignore[call-arg]
            **common,
            account_id=row["account_id"],
            symbol=row["symbol"],
            quantity=row["quantity"],
            average_price=row["average_price"],
            realized_pnl=row["realized_pnl"],
            market_price=row["market_price"],
            unrealized_pnl=row["unrealized_pnl"],
        )
    if record_type is Book:
        bids = tuple(BookLevel(**level) for level in json.loads(row["bids"]))
        asks = tuple(BookLevel(**level) for level in json.loads(row["asks"]))
        return record_type(  # type: ignore[call-arg]
            **common, symbol=row["symbol"], bids=bids, asks=asks, venue=row["venue"]
        )
    if record_type is AccountSnapshot:
        positions = tuple(
            _position_from_json(position) for position in json.loads(row["positions"])
        )
        return record_type(  # type: ignore[call-arg]
            **common,
            account_id=row["account_id"],
            cash=row["cash"],
            equity=row["equity"],
            buying_power=row["buying_power"],
            margin_used=row["margin_used"],
            positions=positions,
        )
    raise TypeError(f"unsupported Parquet record type: {record_type.__name__}")


def _position_from_json(row: Mapping[str, Any]) -> Position:
    return Position(
        event_id=row["event_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        sequence=row["sequence"],
        account_id=row["account_id"],
        symbol=row["symbol"],
        quantity=row["quantity"],
        average_price=row["average_price"],
        realized_pnl=row["realized_pnl"],
        market_price=row["market_price"],
        unrealized_pnl=row["unrealized_pnl"],
    )


__all__ = [
    "EventParquetCodec",
    "ParquetCodec",
    "SymbolParquetCodec",
    "from_parquet",
    "symbols_from_parquet",
    "symbols_to_parquet",
    "to_parquet",
]
