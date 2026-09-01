"""Date-first Hive object paths for market data partitions.

The stable relative path contract is::

    date=YYYY-MM-DD/asset_class=<asset>/data_type=<type>/<symbol>.parquet

Symbols are canonicalized to uppercase and percent-encoded only for use as the
filename. The symbol remains a column in each Arrow schema.
"""

import re
from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote


class AssetClass(StrEnum):
    """Supported Hive ``asset_class`` values."""

    EQUITY = "equity"
    OPTION = "option"
    CRYPTO = "crypto"


class DataType(StrEnum):
    """Supported Hive ``data_type`` values."""

    TRADE = "trade"
    QUOTE = "quote"


type Dataset = tuple[AssetClass, DataType]

PARTITION_PATH_TEMPLATE: Final = (
    "date={date}/asset_class={asset_class}/data_type={data_type}/{symbol}.parquet"
)

SUPPORTED_DATASETS: Final[tuple[Dataset, ...]] = (
    (AssetClass.EQUITY, DataType.TRADE),
    (AssetClass.EQUITY, DataType.QUOTE),
    (AssetClass.OPTION, DataType.TRADE),
    (AssetClass.OPTION, DataType.QUOTE),
    (AssetClass.CRYPTO, DataType.TRADE),
    (AssetClass.CRYPTO, DataType.QUOTE),
)

_SYMBOL_PATTERN: Final = re.compile(r"[A-Z0-9][A-Z0-9._/-]*", re.ASCII)


def symbol_filename(symbol: str) -> str:
    """Return the deterministic Parquet filename for a market symbol.

    Compact OSI symbols and equity punctuation remain readable. Crypto pair
    separators are encoded, so ``BTC/USD`` becomes ``BTC%2FUSD.parquet``
    without introducing another path segment.
    """
    canonical = symbol.strip().upper()
    if canonical in {".", ".."} or _SYMBOL_PATTERN.fullmatch(canonical) is None:
        raise ValueError(f"invalid market symbol: {symbol!r}")
    return f"{quote(canonical, safe='._-')}.parquet"


def partition_path(
    *,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: str,
) -> str:
    """Build a repository-independent, date-first Hive object path."""
    if isinstance(session_date, datetime):
        raise TypeError("session_date must be a date, not a datetime")

    return PurePosixPath(
        f"date={session_date.isoformat()}",
        f"asset_class={asset_class.value}",
        f"data_type={data_type.value}",
        symbol_filename(symbol),
    ).as_posix()
