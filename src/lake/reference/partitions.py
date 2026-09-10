"""Date-first Hive object paths for point-in-time reference datasets.

The stable relative path contract mirrors market data::

    date=YYYY-MM-DD/asset_class=reference/data_type=<type>/<name>.parquet

Classification snapshots are one directory-wide object per knowledge date.
Fundamentals are one object per SEC CIK per ``filed`` knowledge date, so
as-of queries prune whole partitions on ``filed <= as_of``. Object names are
canonicalized to uppercase and reuse the market-data symbol filename rules.
"""

from datetime import date, datetime
from typing import Final

from lake.contracts import AssetClass, DataType, symbol_filename

CLASSIFICATION_OBJECT_NAME: Final = "SNAPSHOT"

REFERENCE_DATA_TYPES: Final[frozenset[DataType]] = frozenset(
    {DataType.CLASSIFICATION, DataType.FUNDAMENTALS}
)

_CIK_DIGITS: Final = 10


def cik_symbol(cik: int) -> str:
    """Return the zero-padded 10-digit catalog symbol for a SEC CIK."""
    if isinstance(cik, bool) or not isinstance(cik, int) or cik <= 0:
        raise ValueError(f"invalid SEC CIK: {cik!r}")
    if cik >= 10**_CIK_DIGITS:
        raise ValueError(f"SEC CIK exceeds {_CIK_DIGITS} digits: {cik}")
    return f"{cik:0{_CIK_DIGITS}d}"


def cik_object_name(cik: int) -> str:
    """Return the self-describing Parquet object name for a SEC CIK."""
    return f"CIK{cik_symbol(cik)}"


def parse_cik_symbol(value: str) -> int:
    """Parse a zero-padded 10-digit catalog symbol back into a SEC CIK."""
    canonical = value.strip()
    if len(canonical) != _CIK_DIGITS or not canonical.isdigit():
        raise ValueError(f"invalid reference CIK symbol: {value!r}")
    return int(canonical)


def reference_partition_path(
    *,
    knowledge_date: date,
    data_type: DataType,
    name: str,
) -> str:
    """Build a repository-independent, date-first Hive object path."""
    if isinstance(knowledge_date, datetime):
        raise TypeError("knowledge_date must be a date, not a datetime")
    reference_type = DataType(data_type)
    if reference_type not in REFERENCE_DATA_TYPES:
        raise ValueError(f"data_type is not a reference dataset: {reference_type}")
    canonical_name = name.strip().upper()
    if not canonical_name:
        raise ValueError("reference object name must not be empty")
    return "/".join(
        (
            f"date={knowledge_date.isoformat()}",
            f"asset_class={AssetClass.REFERENCE.value}",
            f"data_type={reference_type.value}",
            symbol_filename(canonical_name),
        )
    )


__all__ = [
    "CLASSIFICATION_OBJECT_NAME",
    "REFERENCE_DATA_TYPES",
    "cik_object_name",
    "cik_symbol",
    "parse_cik_symbol",
    "reference_partition_path",
]
