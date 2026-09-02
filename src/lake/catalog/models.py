"""Provider-independent catalog records."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import quote

from lake.contracts import AssetClass, DataType


class CoverageStatus(StrEnum):
    """Terminal and replaceable outcomes for one symbol-day dataset."""

    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"


class JobState(StrEnum):
    """States used by asynchronous ensure and backfill jobs."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "complete"
    ERROR = "failed"


class OptionType(StrEnum):
    """The side of an option contract."""

    CALL = "call"
    PUT = "put"


ContractType = OptionType


def canonical_symbol(symbol: str) -> str:
    canonical = symbol.strip().upper()
    if not canonical:
        raise ValueError("symbol must not be empty")
    return canonical


def table_key(value: str) -> str:
    """Encode values used in Azure Table keys without changing their identity."""
    return quote(value, safe="._-~")


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """Coverage metadata for one asset, data type, symbol, and session date."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session_date: date
    status: CoverageStatus
    object_path: str | None = None
    row_count: int = 0
    byte_size: int = 0
    checksum: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.session_date, datetime):
            raise TypeError("session_date must be a date, not a datetime")
        if self.row_count < 0 or self.byte_size < 0:
            raise ValueError("row_count and byte_size must not be negative")
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        object.__setattr__(self, "data_type", DataType(self.data_type))
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "status", CoverageStatus(self.status))

    @property
    def key(self) -> tuple[AssetClass, DataType, str, date]:
        """Return the deterministic logical key for this coverage row."""
        return (self.asset_class, self.data_type, self.symbol, self.session_date)

    @property
    def partition_key(self) -> str:
        return "|".join(
            (
                "coverage",
                self.asset_class.value,
                self.data_type.value,
                table_key(self.symbol),
            )
        )

    @property
    def row_key(self) -> str:
        return self.session_date.isoformat()


@dataclass(frozen=True, slots=True)
class JobRecord:
    """State and audit timestamps for one catalog/ensure job."""

    job_id: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None

    def __post_init__(self) -> None:
        job_id = self.job_id.strip()
        if not job_id:
            raise ValueError("job_id must not be empty")
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "state", JobState(self.state))
        object.__setattr__(self, "created_at", utc_datetime(self.created_at))
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at))

    @property
    def row_key(self) -> str:
        return table_key(self.job_id)


@dataclass(frozen=True, slots=True)
class ContractRecord:
    """Point-in-time option contract metadata held by the contracts catalog."""

    symbol: str
    underlying: str
    expiration_date: date
    strike_price: Decimal
    option_type: OptionType
    multiplier: int = 100
    listed_date: date | None = None
    delisted_date: date | None = None

    def __post_init__(self) -> None:
        if isinstance(self.expiration_date, datetime):
            raise TypeError("expiration_date must be a date, not a datetime")
        if isinstance(self.listed_date, datetime) or isinstance(
            self.delisted_date, datetime
        ):
            raise TypeError("contract lifecycle dates must be dates, not datetimes")
        try:
            strike_price = Decimal(self.strike_price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("strike_price must be a decimal number") from exc
        if strike_price <= 0:
            raise ValueError("strike_price must be positive")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        if (
            self.listed_date is not None
            and self.delisted_date is not None
            and self.listed_date > self.delisted_date
        ):
            raise ValueError("listed_date must be on or before delisted_date")
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "underlying", canonical_symbol(self.underlying))
        object.__setattr__(self, "strike_price", strike_price)
        object.__setattr__(self, "option_type", OptionType(self.option_type))

    @property
    def strike(self) -> Decimal:
        """Short alias used by option selectors."""
        return self.strike_price

    @property
    def right(self) -> OptionType:
        """OSI terminology alias for ``option_type``."""
        return self.option_type


__all__ = [
    "ContractRecord",
    "ContractType",
    "CoverageRecord",
    "CoverageStatus",
    "JobRecord",
    "JobState",
    "OptionType",
]
