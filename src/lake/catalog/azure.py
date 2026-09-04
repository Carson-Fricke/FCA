"""Azure Table mappings and adapters for the catalog ports."""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from lake.catalog.models import (
    ContractRecord,
    CoverageRecord,
    CoverageStatus,
    DelistingType,
    JobRecord,
    JobState,
    ListingRecord,
    ListingType,
    OptionType,
    canonical_symbol,
    table_key,
)
from lake.contracts import AssetClass, DataType

_COVERAGE_PARTITION_PREFIX: Final = "coverage"
_JOB_PARTITION_KEY: Final = "jobs"
_CONTRACT_PARTITION_KEY: Final = "contracts"
_LISTING_PARTITION_PREFIX: Final = "listings"


def _canonical_symbol(symbol: str) -> str:
    return canonical_symbol(symbol)


def _table_key(value: str) -> str:
    """Encode values used in Azure Table keys without changing their identity."""
    return table_key(value)


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


def listing_partition_key(record: ListingRecord) -> str:
    """Build the Azure partition key shared by memory and Table adapters."""
    return listing_partition_for(record.entity_id)


def listing_partition_for(entity_id: str) -> str:
    """Build the Azure partition key for one canonical entity identifier."""
    return "|".join((_LISTING_PARTITION_PREFIX, _table_key(entity_id.strip().upper())))


def listing_to_entity(record: ListingRecord) -> TableEntity:
    """Map a listing episode to an Azure Table-compatible entity."""
    entity = _entity()
    entity.update(
        {
            "PartitionKey": record.partition_key,
            "RowKey": record.row_key,
            "EntityId": record.entity_id,
            "Symbol": record.symbol,
            "ListedDate": record.listed_date.isoformat(),
            "ListingType": record.listing_type.value,
        }
    )
    if record.delisted_date is not None:
        entity["DelistedDate"] = record.delisted_date.isoformat()
    if record.delisting_type is not None:
        entity["DelistingType"] = record.delisting_type.value
    if record.final_price is not None:
        # Azure Tables have no decimal EDM type; strings preserve the exact
        # price value instead of silently converting it to a float.
        entity["FinalPrice"] = str(record.final_price)
    return entity


def listing_from_entity(entity: Mapping[str, object]) -> ListingRecord:
    """Restore a listing episode from an Azure Table entity."""
    delisted_date = _optional_string(entity, "DelistedDate")
    final_price = _optional_string(entity, "FinalPrice")
    delisting_type = _optional_string(entity, "DelistingType")
    return ListingRecord(
        entity_id=str(entity["EntityId"]),
        symbol=str(entity["Symbol"]),
        listed_date=date.fromisoformat(str(entity["ListedDate"])),
        delisted_date=date.fromisoformat(delisted_date) if delisted_date else None,
        listing_type=ListingType(str(entity["ListingType"])),
        delisting_type=DelistingType(delisting_type) if delisting_type else None,
        final_price=Decimal(final_price) if final_price else None,
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


def _listing_query(entity_id: str | None) -> tuple[str, dict[str, str]]:
    if entity_id is not None:
        return (
            "PartitionKey eq @partition",
            {"partition": listing_partition_for(entity_id)},
        )
    return (
        "PartitionKey ge @start and PartitionKey lt @end",
        {"start": "listings|", "end": "listings}"},
    )


class AzureTableListingRepository:
    """Listing repository backed by an Azure Tables ``TableClient``."""

    def __init__(self, table: TableClient) -> None:
        self._table = table

    def get(self, entity_id: str, symbol: str) -> ListingRecord | None:
        entity_id = entity_id.strip().upper()
        if not entity_id:
            raise ValueError("entity_id must not be empty")
        try:
            entity = self._table.get_entity(
                listing_partition_for(entity_id),
                _table_key(_canonical_symbol(symbol)),
            )
        except ResourceNotFoundError:
            return None
        return listing_from_entity(entity)

    def upsert(self, record: ListingRecord) -> ListingRecord:
        self._table.upsert_entity(listing_to_entity(record), mode=UpdateMode.REPLACE)
        return record

    def put(self, record: ListingRecord) -> ListingRecord:
        return self.upsert(record)

    def list(
        self,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ListingRecord, ...]:
        entity_id = entity_id.strip().upper() if entity_id is not None else None
        if entity_id is not None and not entity_id:
            raise ValueError("entity_id must not be empty")
        query_filter, parameters = _listing_query(entity_id)
        records = (
            listing_from_entity(entity)
            for entity in self._table.query_entities(
                query_filter,
                parameters=parameters,
            )
        )
        symbol = _canonical_symbol(symbol) if symbol is not None else None
        return tuple(
            record
            for record in sorted(records, key=lambda item: item.key)
            if (entity_id is None or record.entity_id == entity_id)
            and (symbol is None or record.symbol == symbol)
            and (as_of is None or record.is_known(as_of))
        )


# Short names are convenient for dependency injection while the explicit names
# make the Azure boundary obvious in application wiring.
AzureCatalogRepository = AzureTableCatalogRepository
AzureJobRepository = AzureTableJobRepository
AzureContractRepository = AzureTableContractRepository
AzureListingRepository = AzureTableListingRepository


# Mapping aliases keep the conversion vocabulary discoverable at call sites.
to_coverage_entity = coverage_to_entity
from_coverage_entity = coverage_from_entity
to_job_entity = job_to_entity
from_job_entity = job_from_entity
to_contract_entity = contract_to_entity
from_contract_entity = contract_from_entity
to_listing_entity = listing_to_entity
from_listing_entity = listing_from_entity
