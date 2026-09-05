"""Provider-independent catalog records."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Final
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


class RunState(StrEnum):
    """Lifecycle states for one stored backtest run.

    Deliberately distinct from :class:`JobState`: jobs track asynchronous
    ensure and backfill work, while runs are stored artifacts of completed
    simulations with no launcher, retries, or cancellation semantics. The
    values intentionally mirror the harness ``RunStatus`` vocabulary so run
    rows map between the harness results store and this port without
    translation.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class OptionType(StrEnum):
    """The side of an option contract."""

    CALL = "call"
    PUT = "put"


class ListingType(StrEnum):
    """How one entity came to be listed under a symbol."""

    IPO = "ipo"
    DIRECT = "direct"
    SPINOFF = "spinoff"
    RENAME = "rename"
    TRANSFER = "transfer"
    UNKNOWN = "unknown"


class DelistingType(StrEnum):
    """Why one listing episode ended."""

    RENAME = "rename"
    MERGER = "merger"
    ACQUISITION = "acquisition"
    BANKRUPTCY = "bankruptcy"
    VOLUNTARY = "voluntary"
    REGULATORY = "regulatory"
    UNKNOWN = "unknown"


ContractType = OptionType


def canonical_symbol(symbol: str) -> str:
    canonical = symbol.strip().upper()
    if not canonical:
        raise ValueError("symbol must not be empty")
    return canonical


def table_key(value: str) -> str:
    """Encode values used in Azure Table keys without changing their identity."""
    return quote(value, safe="._-~")


_EMPTY_RUN_METRICS: Final[Mapping[str, str]] = MappingProxyType({})


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
class RunRecord:
    """Catalog row for one stored backtest run.

    Mirrors the harness ``RunCatalogRecord`` row so the results store maps
    to and from this lake-side record without information loss. ``metrics``
    stay string-exact; the harness record keeps the stricter non-empty and
    whitespace rules for metric names and values.
    """

    run_id: str
    status: RunState
    strategy_id: str
    data_start: date | None = None
    data_end: date | None = None
    run_digest: str | None = None
    configuration_digest: str | None = None
    artifact_count: int = 0
    total_bytes: int = 0
    metrics: Mapping[str, str] = _EMPTY_RUN_METRICS

    def __post_init__(self) -> None:
        if isinstance(self.data_start, datetime) or isinstance(self.data_end, datetime):
            raise TypeError("run data bounds must be dates, not datetimes")
        run_id = self.run_id.strip()
        if not run_id:
            raise ValueError("run_id must not be empty")
        strategy_id = self.strategy_id.strip()
        if not strategy_id:
            raise ValueError("strategy_id must not be empty")
        if (
            self.data_start is not None
            and self.data_end is not None
            and self.data_start > self.data_end
        ):
            raise ValueError("data_start must be on or before data_end")
        if self.artifact_count < 0 or self.total_bytes < 0:
            raise ValueError("artifact_count and total_bytes must not be negative")
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        for key, value in self.metrics.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("metric names and values must be strings")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "status", RunState(self.status))
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def row_key(self) -> str:
        return table_key(self.run_id)


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


@dataclass(frozen=True, slots=True)
class ListingRecord:
    """Point-in-time listing episode for one entity under one symbol.

    ``listed_date`` is the first session traded under ``symbol`` and
    ``delisted_date`` is the last; both bounds are inclusive, so a rename is
    modeled as two episodes of one entity with adjacent dates and no overlap.
    Point-in-time consumers must treat facts dated after an as-of instant as
    unknown: :meth:`active_on` keeps a not-yet-known delisting invisible and
    reports the listing as still active.
    """

    entity_id: str
    symbol: str
    listed_date: date
    delisted_date: date | None = None
    listing_type: ListingType = ListingType.UNKNOWN
    delisting_type: DelistingType | None = None
    final_price: Decimal | None = None

    def __post_init__(self) -> None:
        if isinstance(self.listed_date, datetime) or isinstance(
            self.delisted_date, datetime
        ):
            raise TypeError("listing lifecycle dates must be dates, not datetimes")
        entity_id = self.entity_id.strip().upper()
        if not entity_id:
            raise ValueError("entity_id must not be empty")
        if self.delisted_date is not None and self.listed_date > self.delisted_date:
            raise ValueError("listed_date must be on or before delisted_date")
        if self.delisted_date is None and self.delisting_type is not None:
            raise ValueError("delisting_type requires a delisted_date")
        if self.delisted_date is not None and self.delisting_type is None:
            raise ValueError("delisted_date requires a delisting_type")
        if self.final_price is not None:
            if self.delisted_date is None:
                raise ValueError("final_price requires a delisted_date")
            try:
                final_price = Decimal(self.final_price)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("final_price must be a decimal number") from exc
            if final_price <= 0:
                raise ValueError("final_price must be positive")
            object.__setattr__(self, "final_price", final_price)
        object.__setattr__(self, "entity_id", entity_id)
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "listing_type", ListingType(self.listing_type))
        if self.delisting_type is not None:
            object.__setattr__(
                self, "delisting_type", DelistingType(self.delisting_type)
            )

    @property
    def key(self) -> tuple[str, str]:
        """Return the deterministic logical key for this listing episode."""
        return (self.entity_id, self.symbol)

    @property
    def partition_key(self) -> str:
        return "|".join(("listings", table_key(self.entity_id)))

    @property
    def row_key(self) -> str:
        return table_key(self.symbol)

    def is_known(self, as_of: date) -> bool:
        """Return whether the listing itself was known on ``as_of``."""
        if isinstance(as_of, datetime):
            raise TypeError("as_of must be a date, not a datetime")
        return self.listed_date <= as_of

    def active_on(self, day: date, *, as_of: date) -> bool:
        """Return point-in-time activity of this episode on ``day``.

        The listing must be known on ``as_of`` (``listed_date <= as_of``) and
        must cover ``day``.  A delisting dated after ``as_of`` is not yet
        known, so the episode stays active; once the delisting date has
        arrived, the episode covers only sessions on or before it.
        """
        if isinstance(day, datetime) or isinstance(as_of, datetime):
            raise TypeError("day and as_of must be dates, not datetimes")
        if self.listed_date > as_of or day < self.listed_date:
            return False
        if self.delisted_date is None or self.delisted_date > as_of:
            return True
        return day <= self.delisted_date


__all__ = [
    "ContractRecord",
    "ContractType",
    "CoverageRecord",
    "CoverageStatus",
    "DelistingType",
    "JobRecord",
    "JobState",
    "ListingRecord",
    "ListingType",
    "OptionType",
    "RunRecord",
    "RunState",
]
