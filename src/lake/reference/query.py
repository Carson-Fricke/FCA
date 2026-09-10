"""Point-in-time queries over local reference dataset partitions.

As-of semantics are the point-in-time contract: classification returns the
latest snapshot whose knowledge date is on or before ``as_of``; fundamentals
return only facts whose ``filed`` date is on or before ``as_of``, so a
restatement filed later never leaks into an earlier view. Partition
pruning by knowledge date is combined with the row-level ``filed <= as_of``
predicate that defines the point-in-time contract, mirroring the equity
ingest transaction.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from os import PathLike
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from lake.reference.partitions import CLASSIFICATION_OBJECT_NAME
from lake.reference.schemas import CLASSIFICATION_SCHEMA, FUNDAMENTALS_SCHEMA

_PARTITION_DATE: Final = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")

_CLASSIFICATION_PATTERN: Final = (
    "date=*/asset_class=reference/data_type=classification/"
    f"{CLASSIFICATION_OBJECT_NAME}.parquet"
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

_FACT_RECENCY: Final = ("filed", "accn")

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
    "classification_snapshot",
    "fundamentals_facts",
    "latest_fundamentals",
]
