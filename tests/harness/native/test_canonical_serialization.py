"""Byte-identical canonical serialization between the Python codecs and Zig.

The native record serializer reproduces the trade row produced by
``harness.core.types.codecs``: the codecs' row mapping with compact JSON
separators and the timestamp rendered with ``datetime.isoformat()`` (the
codecs' convention for timestamps inside embedded JSON rows). Decimal fields
carry the pinned scale-18 fixed-point representation and are rendered exactly
like ``str(Decimal)``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from harness._native import DECIMAL_SCALE, NativeCore
from harness.core.symbols import OsiSymbol, Symbol
from harness.core.types.codecs import _row
from harness.core.types.events import TradeEvent

_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)

_TIMESTAMP_NS: int = 1_704_164_645_123_456_000
_WHOLE_SECOND_NS: int = 1_704_164_645_000_000_000


def _timestamp(nanoseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=nanoseconds // 1000)


_RECORDS: tuple[TradeEvent, ...] = (
    TradeEvent(
        event_id="evt-1",
        timestamp=_timestamp(_TIMESTAMP_NS),
        sequence=9,
        symbol=Symbol("aapl"),
        price=Decimal("150.250000000000000000"),
        quantity=Decimal("10.000000000000000000"),
        venue="XNAS",
        trade_id="t-7",
    ),
    TradeEvent(
        event_id="evt-2",
        timestamp=_timestamp(_TIMESTAMP_NS),
        sequence=0,
        symbol=OsiSymbol("AAPL240119C00150000"),
        price=Decimal("1E-18"),
        quantity=Decimal("99.999999999999999999"),
        venue=None,
        trade_id=None,
    ),
    TradeEvent(
        event_id="evt-3",
        timestamp=_timestamp(_WHOLE_SECOND_NS),
        sequence=0,
        symbol=Symbol("BRK.B"),
        price=Decimal("0.000000000000015025"),
        quantity=Decimal("1E-18"),
        venue="XNAS",
        trade_id=None,
    ),
)


def _timestamp_ns(record: TradeEvent) -> int:
    return int((record.timestamp - _EPOCH) // timedelta(microseconds=1)) * 1000


def _unscaled(value: Decimal) -> int:
    return int(value.scaleb(DECIMAL_SCALE))


def expected_trade_row_bytes(record: TradeEvent) -> bytes:
    """Build the canonical bytes from the Python codecs' own row mapping."""
    row = _row(record)
    row = {
        **row,
        "timestamp": cast(datetime, row["timestamp"]).isoformat(),
    }
    return json.dumps(row, separators=(",", ":")).encode("utf-8")


def test_trade_rows_serialize_to_identical_bytes(native_core: NativeCore) -> None:
    for record in _RECORDS:
        native_bytes = native_core.trade_row_json(
            event_id=record.event_id,
            timestamp_ns=_timestamp_ns(record),
            sequence=record.sequence,
            symbol=str(record.symbol),
            price_unscaled=_unscaled(record.price),
            quantity_unscaled=_unscaled(record.quantity),
            venue=record.venue,
            trade_id=record.trade_id,
        )
        assert native_bytes == expected_trade_row_bytes(record)


def test_trade_row_serializer_rejects_non_canonical_input(
    native_core: NativeCore,
) -> None:
    with pytest.raises(Exception, match="invalid_symbol"):
        native_core.trade_row_json(
            event_id="evt-4",
            timestamp_ns=_WHOLE_SECOND_NS,
            sequence=0,
            symbol="aapl",
            price_unscaled=0,
            quantity_unscaled=0,
            venue=None,
            trade_id=None,
        )


def test_decimal_text_matches_the_exact_record_values(
    native_core: NativeCore,
) -> None:
    for record in _RECORDS:
        for value in (record.price, record.quantity):
            assert native_core.decimal_text(_unscaled(value)) == str(value)
