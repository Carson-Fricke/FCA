"""Catalog domain records and repository adapters.

The repository protocols are deliberately independent of Azure.  The in-memory
implementations provide deterministic behavior for unit tests, while the Azure
implementations accept an already-configured ``TableClient`` and only translate
records to and from Table entities.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Protocol
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from lake.partition_paths import AssetClass, DataType


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

_COVERAGE_PARTITION_PREFIX: Final = "coverage"
_JOB_PARTITION_KEY: Final = "jobs"
_CONTRACT_PARTITION_KEY: Final = "contracts"


def _canonical_symbol(symbol: str) -> str:
    canonical = symbol.strip().upper()
    if not canonical:
        raise ValueError("symbol must not be empty")
    return canonical


def _table_key(value: str) -> str:
    """Encode values used in Azure Table keys without changing their identity."""
    return quote(value, safe="._-~")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _entity() -> TableEntity:
    return TableEntity()


def _optional_string(entity: Mapping[str, object], name: str) -> str | None:
    value = entity.get(name)
    return None if value is None else str(value)


def _integer(entity: Mapping[str, object], name: str, default: int) -> int:
    value = entity.get(name)
    if value is None:
        return default
    return value if isinstance(value, int) else int(str(value))


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
        object.__setattr__(self, "symbol", _canonical_symbol(self.symbol))
        object.__setattr__(self, "status", CoverageStatus(self.status))

    @property
    def key(self) -> tuple[AssetClass, DataType, str, date]:
        """Return the deterministic logical key for this coverage row."""
        return (self.asset_class, self.data_type, self.symbol, self.session_date)

    @property
    def partition_key(self) -> str:
        return coverage_partition_key(self)

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
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at))
        object.__setattr__(self, "updated_at", _utc_datetime(self.updated_at))

    @property
    def row_key(self) -> str:
        return _table_key(self.job_id)


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
        object.__setattr__(self, "symbol", _canonical_symbol(self.symbol))
        object.__setattr__(self, "underlying", _canonical_symbol(self.underlying))
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


class CatalogRepository(Protocol):
    """Persistence contract for symbol-day coverage metadata."""

    def get(
        self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
    ) -> CoverageRecord | None:
        """Return one coverage row, or ``None`` when it has not been recorded."""

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        """Replace the row for the record's logical key."""

    def list(
        self,
        *,
        asset_class: AssetClass | None = None,
        data_type: DataType | None = None,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CoverageRecord, ...]:
        """Return matching rows in deterministic key order."""


class JobRepository(Protocol):
    """Persistence contract for asynchronous job state."""

    def get(self, job_id: str) -> JobRecord | None:
        """Return one job, or ``None`` when it has not been recorded."""

    def upsert(self, record: JobRecord) -> JobRecord:
        """Replace the job's current state."""

    def list(self, *, state: JobState | None = None) -> tuple[JobRecord, ...]:
        """Return jobs in deterministic job-id order."""


class ContractRepository(Protocol):
    """Persistence contract for option contract metadata."""

    def get(self, symbol: str) -> ContractRecord | None:
        """Return a contract by canonical symbol."""

    def upsert(self, record: ContractRecord) -> ContractRecord:
        """Replace the contract for its symbol."""

    def list(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ContractRecord, ...]:
        """Return contracts, optionally restricted to an active date."""


def coverage_partition_key(record: CoverageRecord) -> str:
    """Build the Azure partition key shared by memory and Table adapters."""
    return "|".join(
        (
            _COVERAGE_PARTITION_PREFIX,
            record.asset_class.value,
            record.data_type.value,
            _table_key(record.symbol),
        )
    )


def coverage_to_entity(record: CoverageRecord) -> TableEntity:
    """Map coverage metadata to an Azure Table-compatible entity."""
    entity = _entity()
    entity.update(
        {
            "PartitionKey": record.partition_key,
            "RowKey": record.row_key,
            "AssetClass": record.asset_class.value,
            "DataType": record.data_type.value,
            "Symbol": record.symbol,
            "SessionDate": record.session_date.isoformat(),
            "Status": record.status.value,
            "RowCount": record.row_count,
            "ByteSize": record.byte_size,
        }
    )
    if record.object_path is not None:
        entity["ObjectPath"] = record.object_path
    if record.checksum is not None:
        entity["Checksum"] = record.checksum
    if record.error_message is not None:
        entity["ErrorMessage"] = record.error_message
    return entity


def coverage_from_entity(entity: Mapping[str, object]) -> CoverageRecord:
    """Restore a coverage record from an Azure Table entity."""
    return CoverageRecord(
        asset_class=AssetClass(str(entity["AssetClass"])),
        data_type=DataType(str(entity["DataType"])),
        symbol=str(entity["Symbol"]),
        session_date=date.fromisoformat(str(entity["SessionDate"])),
        status=CoverageStatus(str(entity["Status"])),
        object_path=_optional_string(entity, "ObjectPath"),
        row_count=_integer(entity, "RowCount", 0),
        byte_size=_integer(entity, "ByteSize", 0),
        checksum=_optional_string(entity, "Checksum"),
        error_message=_optional_string(entity, "ErrorMessage"),
    )


def job_to_entity(record: JobRecord) -> TableEntity:
    """Map job state and UTC timestamps to an Azure Table-compatible entity."""
    entity = _entity()
    entity.update(
        {
            "PartitionKey": _JOB_PARTITION_KEY,
            "RowKey": record.row_key,
            "JobId": record.job_id,
            "State": record.state.value,
            "CreatedAt": record.created_at,
            "UpdatedAt": record.updated_at,
        }
    )
    if record.error_message is not None:
        entity["ErrorMessage"] = record.error_message
    return entity


def job_from_entity(entity: Mapping[str, object]) -> JobRecord:
    """Restore a job record from an Azure Table entity."""
    created_at = entity["CreatedAt"]
    updated_at = entity["UpdatedAt"]
    if not isinstance(created_at, datetime) or not isinstance(updated_at, datetime):
        raise TypeError("job entity timestamps must be datetime values")
    return JobRecord(
        job_id=str(entity["JobId"]),
        state=JobState(str(entity["State"])),
        created_at=created_at,
        updated_at=updated_at,
        error_message=_optional_string(entity, "ErrorMessage"),
    )


def contract_to_entity(record: ContractRecord) -> TableEntity:
    """Map contract metadata to an Azure Table-compatible entity."""
    entity = _entity()
    entity.update(
        {
            "PartitionKey": _CONTRACT_PARTITION_KEY,
            "RowKey": _table_key(record.symbol),
            "Symbol": record.symbol,
            "Underlying": record.underlying,
            "ExpirationDate": record.expiration_date.isoformat(),
            # Azure Tables have no decimal EDM type; strings preserve the exact
            # strike value instead of silently converting it to a float.
            "StrikePrice": str(record.strike_price),
            "OptionType": record.option_type.value,
            "Multiplier": record.multiplier,
        }
    )
    if record.listed_date is not None:
        entity["ListedDate"] = record.listed_date.isoformat()
    if record.delisted_date is not None:
        entity["DelistedDate"] = record.delisted_date.isoformat()
    return entity


def contract_from_entity(entity: Mapping[str, object]) -> ContractRecord:
    """Restore a contract record from an Azure Table entity."""
    listed_date = _optional_string(entity, "ListedDate")
    delisted_date = _optional_string(entity, "DelistedDate")
    return ContractRecord(
        symbol=str(entity["Symbol"]),
        underlying=str(entity["Underlying"]),
        expiration_date=date.fromisoformat(str(entity["ExpirationDate"])),
        strike_price=Decimal(str(entity["StrikePrice"])),
        option_type=OptionType(str(entity["OptionType"])),
        multiplier=_integer(entity, "Multiplier", 100),
        listed_date=date.fromisoformat(listed_date) if listed_date else None,
        delisted_date=date.fromisoformat(delisted_date) if delisted_date else None,
    )


class InMemoryCatalogRepository:
    """Deterministic process-local catalog repository."""

    def __init__(self, records: Iterable[CoverageRecord] = ()) -> None:
        self._records = {record.key: record for record in records}

    def get(
        self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
    ) -> CoverageRecord | None:
        key = (
            AssetClass(asset_class),
            DataType(data_type),
            _canonical_symbol(symbol),
            session_date,
        )
        return self._records.get(key)

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        self._records[record.key] = record
        return record

    def put(self, record: CoverageRecord) -> CoverageRecord:
        """Alias for callers that use put semantics for repository writes."""
        return self.upsert(record)

    def list(
        self,
        *,
        asset_class: AssetClass | None = None,
        data_type: DataType | None = None,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CoverageRecord, ...]:
        asset_class = AssetClass(asset_class) if asset_class is not None else None
        data_type = DataType(data_type) if data_type is not None else None
        symbol = _canonical_symbol(symbol) if symbol is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("start must be on or before end")
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.key)
            if (asset_class is None or record.asset_class == asset_class)
            and (data_type is None or record.data_type == data_type)
            and (symbol is None or record.symbol == symbol)
            and (start is None or record.session_date >= start)
            and (end is None or record.session_date <= end)
        )


class InMemoryJobRepository:
    """Deterministic process-local job repository."""

    def __init__(self, records: Iterable[JobRecord] = ()) -> None:
        self._records = {record.job_id: record for record in records}

    def get(self, job_id: str) -> JobRecord | None:
        return self._records.get(job_id.strip())

    def upsert(self, record: JobRecord) -> JobRecord:
        self._records[record.job_id] = record
        return record

    def put(self, record: JobRecord) -> JobRecord:
        return self.upsert(record)

    def list(self, *, state: JobState | None = None) -> tuple[JobRecord, ...]:
        state = JobState(state) if state is not None else None
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.job_id)
            if state is None or record.state == state
        )


class InMemoryContractRepository:
    """Deterministic process-local contracts repository."""

    def __init__(self, records: Iterable[ContractRecord] = ()) -> None:
        self._records = {record.symbol: record for record in records}

    def get(self, symbol: str) -> ContractRecord | None:
        return self._records.get(_canonical_symbol(symbol))

    def upsert(self, record: ContractRecord) -> ContractRecord:
        self._records[record.symbol] = record
        return record

    def put(self, record: ContractRecord) -> ContractRecord:
        return self.upsert(record)

    def list(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ContractRecord, ...]:
        underlying = _canonical_symbol(underlying) if underlying is not None else None
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.symbol)
            if (underlying is None or record.underlying == underlying)
            and _is_active(record, as_of)
        )


def _is_active(record: ContractRecord, as_of: date | None) -> bool:
    if as_of is None:
        return True
    return (record.listed_date is None or as_of >= record.listed_date) and (
        record.delisted_date is None or as_of <= record.delisted_date
    )


class AzureTableCatalogRepository:
    """Catalog repository backed by an Azure Tables ``TableClient``."""

    def __init__(self, table: TableClient) -> None:
        self._table = table

    def get(
        self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
    ) -> CoverageRecord | None:
        record = CoverageRecord(
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
            session_date=session_date,
            status=CoverageStatus.EMPTY,
        )
        try:
            entity = self._table.get_entity(record.partition_key, record.row_key)
        except ResourceNotFoundError:
            return None
        return coverage_from_entity(entity)

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        self._table.upsert_entity(coverage_to_entity(record), mode=UpdateMode.REPLACE)
        return record

    def put(self, record: CoverageRecord) -> CoverageRecord:
        return self.upsert(record)

    def list(
        self,
        *,
        asset_class: AssetClass | None = None,
        data_type: DataType | None = None,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CoverageRecord, ...]:
        if start is not None and end is not None and start > end:
            raise ValueError("start must be on or before end")
        query_filter, parameters = _coverage_query(asset_class, data_type, symbol)
        records = (
            coverage_from_entity(entity)
            for entity in self._table.query_entities(
                query_filter,
                parameters=parameters,
            )
        )
        return tuple(
            record
            for record in sorted(records, key=lambda item: item.key)
            if (asset_class is None or record.asset_class == AssetClass(asset_class))
            and (data_type is None or record.data_type == DataType(data_type))
            and (symbol is None or record.symbol == _canonical_symbol(symbol))
            and (start is None or record.session_date >= start)
            and (end is None or record.session_date <= end)
        )


def _coverage_partition_for_filters(
    asset_class: AssetClass | None,
    data_type: DataType | None,
    symbol: str | None,
) -> str:
    if asset_class is None or data_type is None or symbol is None:
        # A range query covers the prefixed partitions for an unqualified scan.
        return _COVERAGE_PARTITION_PREFIX
    return "|".join(
        (
            _COVERAGE_PARTITION_PREFIX,
            AssetClass(asset_class).value,
            DataType(data_type).value,
            _table_key(_canonical_symbol(symbol)),
        )
    )


def _coverage_query(
    asset_class: AssetClass | None,
    data_type: DataType | None,
    symbol: str | None,
) -> tuple[str, dict[str, str]]:
    if asset_class is not None and data_type is not None and symbol is not None:
        return (
            "PartitionKey eq @partition",
            {
                "partition": _coverage_partition_for_filters(
                    asset_class, data_type, symbol
                )
            },
        )
    return (
        "PartitionKey ge @start and PartitionKey lt @end",
        {"start": "coverage|", "end": "coverage}"},
    )


class AzureTableJobRepository:
    """Job repository backed by an Azure Tables ``TableClient``."""

    def __init__(self, table: TableClient) -> None:
        self._table = table

    def get(self, job_id: str) -> JobRecord | None:
        job_id = job_id.strip()
        try:
            entity = self._table.get_entity(_JOB_PARTITION_KEY, _table_key(job_id))
        except ResourceNotFoundError:
            return None
        return job_from_entity(entity)

    def upsert(self, record: JobRecord) -> JobRecord:
        self._table.upsert_entity(job_to_entity(record), mode=UpdateMode.REPLACE)
        return record

    def put(self, record: JobRecord) -> JobRecord:
        return self.upsert(record)

    def list(self, *, state: JobState | None = None) -> tuple[JobRecord, ...]:
        records = (
            job_from_entity(entity)
            for entity in self._table.query_entities(
                "PartitionKey eq @partition",
                parameters={"partition": _JOB_PARTITION_KEY},
            )
        )
        state = JobState(state) if state is not None else None
        return tuple(
            record
            for record in sorted(records, key=lambda item: item.job_id)
            if state is None or record.state == state
        )


class AzureTableContractRepository:
    """Contract repository backed by an Azure Tables ``TableClient``."""

    def __init__(self, table: TableClient) -> None:
        self._table = table

    def get(self, symbol: str) -> ContractRecord | None:
        try:
            entity = self._table.get_entity(
                _CONTRACT_PARTITION_KEY, _table_key(_canonical_symbol(symbol))
            )
        except ResourceNotFoundError:
            return None
        return contract_from_entity(entity)

    def upsert(self, record: ContractRecord) -> ContractRecord:
        self._table.upsert_entity(contract_to_entity(record), mode=UpdateMode.REPLACE)
        return record

    def put(self, record: ContractRecord) -> ContractRecord:
        return self.upsert(record)

    def list(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ContractRecord, ...]:
        records = (
            contract_from_entity(entity)
            for entity in self._table.query_entities(
                "PartitionKey eq @partition",
                parameters={"partition": _CONTRACT_PARTITION_KEY},
            )
        )
        underlying = _canonical_symbol(underlying) if underlying is not None else None
        return tuple(
            record
            for record in sorted(records, key=lambda item: item.symbol)
            if (underlying is None or record.underlying == underlying)
            and _is_active(record, as_of)
        )


# Short names are convenient for dependency injection while the explicit names
# make the Azure boundary obvious in application wiring.
AzureCatalogRepository = AzureTableCatalogRepository
AzureJobRepository = AzureTableJobRepository
AzureContractRepository = AzureTableContractRepository


# Mapping aliases keep the conversion vocabulary discoverable at call sites.
to_coverage_entity = coverage_to_entity
from_coverage_entity = coverage_from_entity
to_job_entity = job_to_entity
from_job_entity = job_from_entity
to_contract_entity = contract_to_entity
from_contract_entity = contract_from_entity
