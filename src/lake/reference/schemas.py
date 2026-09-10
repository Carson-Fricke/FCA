"""Versioned PyArrow schemas for point-in-time reference datasets.

Reference datasets do not use the market-data schema registry in
:mod:`lake.contracts.schemas` because their partitions are not per symbol
sessions. Increment ``REFERENCE_SCHEMA_VERSION`` for any field, type, order,
nullability, or semantic change.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

import pyarrow as pa

REFERENCE_SCHEMA_VERSION: Final = "1"

_DECIMAL: Final = pa.decimal128(38, 18)
_UTC_TIMESTAMP: Final = pa.timestamp("ns", tz="UTC")


def _field(name: str, data_type: pa.DataType, *, nullable: bool = False) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


def _schema(name: str, fields: Sequence[pa.Field]) -> pa.Schema:
    return pa.schema(
        fields,
        metadata={
            b"lake.schema": name.encode("ascii"),
            b"lake.schema.version": REFERENCE_SCHEMA_VERSION.encode("ascii"),
        },
    )


CLASSIFICATION_SCHEMA: Final = _schema(
    "reference.classification",
    (
        _field("file_creation_time", _UTC_TIMESTAMP),
        _field("symbol", pa.string()),
        _field("security_name", pa.string()),
        _field("listing_exchange", pa.string()),
        _field("market_category", pa.string()),
        _field("etf", pa.bool_()),
        _field("round_lot_size", _DECIMAL),
        _field("test_issue", pa.bool_()),
        _field("financial_status", pa.string(), nullable=True),
    ),
)

FUNDAMENTALS_SCHEMA: Final = _schema(
    "reference.fundamentals",
    (
        _field("cik", pa.uint64()),
        _field("taxonomy", pa.string()),
        _field("tag", pa.string()),
        _field("unit", pa.string()),
        _field("period_end", pa.date32()),
        _field("fy", pa.int32(), nullable=True),
        _field("fp", pa.string(), nullable=True),
        _field("form", pa.string()),
        _field("accn", pa.string()),
        _field("filed", pa.date32()),
        _field("frame", pa.string(), nullable=True),
        _field("val", _DECIMAL),
    ),
)

_CLASSIFICATION_SORT_KEY: Final = ("symbol",)

_FUNDAMENTALS_SORT_KEY: Final = (
    "cik",
    "taxonomy",
    "tag",
    "unit",
    "period_end",
    "filed",
    "accn",
    "fy",
    "fp",
    "form",
    "frame",
    "val",
)


def classification_table(
    records: Iterable[Mapping[str, object]],
) -> pa.Table:
    """Normalize classification records into deterministic partition order."""
    return _table(records, CLASSIFICATION_SCHEMA, _CLASSIFICATION_SORT_KEY)


def fundamentals_table(
    records: Iterable[Mapping[str, object]],
) -> pa.Table:
    """Normalize fundamentals records into deterministic partition order."""
    return _table(records, FUNDAMENTALS_SCHEMA, _FUNDAMENTALS_SORT_KEY)


def _table(
    records: Iterable[Mapping[str, object]],
    schema: pa.Schema,
    sort_key: Sequence[str],
) -> pa.Table:
    normalized_rows: list[dict[str, object]] = []
    for row_number, record in enumerate(records):
        normalized: dict[str, object] = dict(record)
        for field in schema:
            if field.name not in normalized or normalized[field.name] is None:
                if not field.nullable:
                    raise ValueError(
                        f"record {row_number} is missing required field {field.name!r}"
                    )
                normalized[field.name] = None
                continue
            normalized[field.name] = _normalize_value(
                normalized[field.name], field.type, field.name
            )
        normalized_rows.append(normalized)
    normalized_rows.sort(
        key=lambda row: tuple(_sortable(row[name]) for name in sort_key)
    )
    return pa.Table.from_pylist(normalized_rows, schema=schema)


def _sortable(value: object) -> tuple[bool, object]:
    if isinstance(value, Decimal):
        return (False, value)
    if isinstance(value, (date, datetime)):
        return (False, value)
    if value is None:
        return (True, "")
    return (False, value)


def _normalize_value(value: object, data_type: pa.DataType, field_name: str) -> object:
    if pa.types.is_timestamp(data_type):
        return _timestamp_nanoseconds(value, field_name)
    if pa.types.is_date(data_type):
        return _date(value, field_name)
    if pa.types.is_decimal(data_type):
        return _decimal(value, field_name)
    if pa.types.is_boolean(data_type):
        if not isinstance(value, bool):
            raise TypeError(f"{field_name} must be a boolean")
        return value
    if pa.types.is_integer(data_type):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{field_name} must be an integer")
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


_NANOSECONDS_PER_SECOND: Final = 1_000_000_000


def _timestamp_nanoseconds(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an aware datetime or UTC nanoseconds")
    if isinstance(value, int):
        return value
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be an aware datetime or UTC nanoseconds")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    elapsed = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        elapsed.days * 86_400 + elapsed.seconds
    ) * _NANOSECONDS_PER_SECOND + elapsed.microseconds * 1_000


def _date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO-8601 date") from error
    raise TypeError(f"{field_name} must be a date or ISO-8601 string")


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an exact decimal value, not float")
    if isinstance(value, float):
        value = repr(value)
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{field_name} must be an exact decimal value")
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an exact decimal value") from error
    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal value")
    return decimal_value


__all__ = [
    "CLASSIFICATION_SCHEMA",
    "FUNDAMENTALS_SCHEMA",
    "REFERENCE_SCHEMA_VERSION",
    "classification_table",
    "fundamentals_table",
]
