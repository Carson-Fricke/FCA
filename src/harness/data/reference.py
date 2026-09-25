"""Point-in-time reference queries and schemas over Parquet partitions.

The harness owns the reference-data read surface consumed by Python products:
the thesis screener context resolves classification and fundamentals facts
through this module rather than importing the lake service directly. As-of
semantics are the point-in-time contract: classification returns the latest
snapshot whose knowledge date is on or before ``as_of``; fundamentals return
only facts whose ``filed`` date is on or before ``as_of``, so a restatement
filed later never leaks into an earlier view. Partition pruning by knowledge
date is combined with the row-level ``filed <= as_of`` predicate that defines
the point-in-time contract, mirroring the equity ingest transaction.

The versioned PyArrow schemas and table builders here normalize records into
deterministic partition order and stamp the ``lake.schema`` metadata, so
partitions written through :func:`classification_table` and
:func:`fundamentals_table` stay byte-compatible with the lake reference
ingest. Increment ``REFERENCE_SCHEMA_VERSION`` for any field, type, order,
nullability, or semantic change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from os import PathLike
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

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
        key=lambda row: tuple(_table_sortable(row[name]) for name in sort_key)
    )
    return pa.Table.from_pylist(normalized_rows, schema=schema)


def _table_sortable(value: object) -> tuple[bool, object]:
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


_PARTITION_DATE: Final = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")

_CLASSIFICATION_OBJECT_NAME: Final = "SNAPSHOT"

_CLASSIFICATION_PATTERN: Final = (
    "date=*/asset_class=reference/data_type=classification/"
    f"{_CLASSIFICATION_OBJECT_NAME}.parquet"
)

_FUNDAMENTALS_PATTERN: Final = (
    "date=*/asset_class=reference/data_type=fundamentals/*.parquet"
)

_ROW_FIELDS: Final = (
    "file_creation_time",
    "symbol",
    "security_name",
    "listing_exchange",
    "market_category",
)

_FACT_FIELDS: Final = tuple(field.name for field in FUNDAMENTALS_SCHEMA)

_FACT_IDENTITY: Final = ("cik", "taxonomy", "tag", "unit", "period_end")

_AS_OF_TYPE_ERROR: Final = "as_of must be a date, not a datetime"


def classification_snapshot(
    root: str | PathLike[str], as_of: date
) -> tuple[dict[str, object], ...]:
    """Return the rows of the newest snapshot known on or before ``as_of``.

    Snapshots are self-contained daily objects, so partition pruning on the
    knowledge date is the whole selection: a later snapshot contributes
    nothing to an earlier as-of and no earlier row survives its replacement.
    """
    _require_date(as_of)
    latest = _latest_partition(root, _CLASSIFICATION_PATTERN, as_of)
    if latest is None:
        return ()
    rows = _read_rows(latest, CLASSIFICATION_SCHEMA)
    return tuple(sorted(rows, key=lambda row: tuple(row[name] for name in _ROW_FIELDS)))


def fundamentals_facts(
    root: str | PathLike[str],
    as_of: date,
    *,
    cik: int | None = None,
    taxonomy: str | None = None,
    tag: str | None = None,
    unit: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Return every fact row visible at ``as_of``.

    Only partitions whose knowledge date (the ``filed`` date) is on or before
    ``as_of`` are read, and each surviving row is re-checked against the same
    ``filed <= as_of`` predicate so the contract holds even for objects that
    mix knowledge dates. ``cik``, ``taxonomy``, ``tag``, and ``unit`` narrow
    the result when supplied.
    """
    _require_date(as_of)
    rows: list[dict[str, object]] = []
    for path in _pruned_partitions(root, _FUNDAMENTALS_PATTERN, as_of):
        for row in _read_rows(path, FUNDAMENTALS_SCHEMA):
            if _visible(row, as_of, cik=cik, taxonomy=taxonomy, tag=tag, unit=unit):
                rows.append(row)
    return tuple(sorted(rows, key=_fact_sort_key))


def latest_fundamentals(
    root: str | PathLike[str],
    as_of: date,
    *,
    cik: int | None = None,
    taxonomy: str | None = None,
    tag: str | None = None,
    unit: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Return one fact per XBRL identity, as known at ``as_of``.

    The identity is ``(cik, taxonomy, tag, unit, period_end)``; among the
    visible rows the one with the greatest ``(filed, accn)`` wins, so a
    restatement replaces the original only from its own filing date onward
    and earlier as-of instants resolve to the originally filed value.
    """
    facts = fundamentals_facts(
        root, as_of, cik=cik, taxonomy=taxonomy, tag=tag, unit=unit
    )
    latest: dict[tuple[object, ...], dict[str, object]] = {}
    for row in facts:
        identity = tuple(row[name] for name in _FACT_IDENTITY)
        current = latest.get(identity)
        if current is None or _recency(row) > _recency(current):
            latest[identity] = row
    return tuple(sorted(latest.values(), key=_fact_sort_key))


def _require_date(as_of: date) -> None:
    if isinstance(as_of, datetime):
        raise TypeError(_AS_OF_TYPE_ERROR)
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a date")


def _pruned_partitions(
    root: str | PathLike[str], pattern: str, as_of: date
) -> tuple[Path, ...]:
    base = Path(root)
    if not base.is_dir():
        return ()
    pruned: list[tuple[date, Path]] = []
    for path in base.glob(pattern):
        knowledge_date = _knowledge_date(base, path)
        if knowledge_date is not None and knowledge_date <= as_of:
            pruned.append((knowledge_date, path))
    return tuple(path for _, path in sorted(pruned))


def _latest_partition(
    root: str | PathLike[str], pattern: str, as_of: date
) -> Path | None:
    pruned = _pruned_partitions(root, pattern, as_of)
    return pruned[-1] if pruned else None


def _knowledge_date(base: Path, path: Path) -> date | None:
    relative = path.relative_to(base)
    if not relative.parts:
        return None
    match = _PARTITION_DATE.fullmatch(relative.parts[0])
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _read_rows(path: Path, schema: pa.Schema) -> list[dict[str, object]]:
    table = pq.read_table(path)
    if not table.schema.equals(schema, check_metadata=False):
        raise ValueError(f"reference object has an unexpected schema: {path}")
    return table.to_pylist()


def _visible(
    row: dict[str, object],
    as_of: date,
    *,
    cik: int | None,
    taxonomy: str | None,
    tag: str | None,
    unit: str | None,
) -> bool:
    if _filed_of(row) > as_of:
        return False
    if cik is not None and row["cik"] != cik:
        return False
    if taxonomy is not None and row["taxonomy"] != taxonomy:
        return False
    if tag is not None and row["tag"] != tag:
        return False
    return not (unit is not None and row["unit"] != unit)


def _filed_of(row: dict[str, object]) -> date:
    filed = row["filed"]
    if not isinstance(filed, date) or isinstance(filed, datetime):
        raise ValueError("reference fact row has a non-date filed value")
    return filed


def _recency(row: dict[str, object]) -> tuple[date, str]:
    accn = row["accn"]
    if not isinstance(accn, str):
        raise ValueError("reference fact row has a non-string accn value")
    return (_filed_of(row), accn)


def _fact_sort_key(row: dict[str, object]) -> tuple[object, ...]:
    return tuple(_sortable(row[name]) for name in _FACT_FIELDS)


def _sortable(value: object) -> tuple[bool, object]:
    return (value is None, "" if value is None else value)


__all__ = [
    "CLASSIFICATION_SCHEMA",
    "FUNDAMENTALS_SCHEMA",
    "REFERENCE_SCHEMA_VERSION",
    "classification_snapshot",
    "classification_table",
    "fundamentals_facts",
    "fundamentals_table",
    "latest_fundamentals",
]
