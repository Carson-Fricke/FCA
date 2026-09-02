from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from lake.catalog import (
    AzureTableCatalogRepository,
    AzureTableContractRepository,
    AzureTableJobRepository,
    CatalogRepository,
    ContractRecord,
    ContractRepository,
    ContractType,
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
    InMemoryContractRepository,
    InMemoryJobRepository,
    JobRecord,
    JobRepository,
    JobState,
    contract_from_entity,
    contract_to_entity,
    coverage_from_entity,
    coverage_to_entity,
    job_from_entity,
    job_to_entity,
)
from lake.contracts import AssetClass, DataType


class FakeTable:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], TableEntity] = {}
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def get_entity(self, partition_key: str, row_key: str) -> TableEntity:
        try:
            return self.entities[(partition_key, row_key)]
        except KeyError:
            raise ResourceNotFoundError("missing") from None

    def upsert_entity(self, entity: TableEntity, mode: UpdateMode) -> None:
        assert mode is UpdateMode.REPLACE
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = TableEntity(entity)

    def query_entities(
        self,
        query_filter: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> list[TableEntity]:
        self.queries.append((query_filter, parameters))
        if parameters is None:
            return list(self.entities.values())
        partition = parameters.get("partition")
        if partition is not None:
            return [
                entity
                for entity in self.entities.values()
                if entity["PartitionKey"] == partition
            ]
        return [
            entity
            for entity in self.entities.values()
            if str(entity["PartitionKey"]).startswith(parameters["start"])
            and str(entity["PartitionKey"]) < parameters["end"]
        ]


def _coverage(status: CoverageStatus = CoverageStatus.COMPLETE) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol=" aapl ",
        session_date=date(2024, 1, 19),
        status=status,
        object_path="date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet",
        row_count=12,
        byte_size=3456,
        checksum="sha256:abc",
        error_message="source unavailable" if status is CoverageStatus.ERROR else None,
    )


def _job(state: JobState = JobState.QUEUED) -> JobRecord:
    return JobRecord(
        job_id=" job/2024-01-19 ",
        state=state,
        created_at=datetime(2024, 1, 19, 12, tzinfo=UTC),
        updated_at=datetime(2024, 1, 19, 12, 30, tzinfo=UTC),
        error_message="failed to fetch" if state is JobState.FAILED else None,
    )


def _contract() -> ContractRecord:
    return ContractRecord(
        symbol=" aapl240119c00150000 ",
        underlying=" aapl ",
        expiration_date=date(2024, 1, 19),
        strike_price=Decimal("150.125000000000000001"),
        option_type=ContractType.CALL,
        listed_date=date(2023, 1, 1),
        delisted_date=date(2024, 1, 19),
    )


@pytest.mark.parametrize("status", list(CoverageStatus))
def test_coverage_status_round_trips_through_entity(status: CoverageStatus) -> None:
    record = _coverage(status)

    assert coverage_from_entity(coverage_to_entity(record)) == record


@pytest.mark.parametrize("status", list(CoverageStatus))
def test_coverage_status_round_trips_through_memory(status: CoverageStatus) -> None:
    record = _coverage(status)
    repository = InMemoryCatalogRepository()

    repository.upsert(record)

    assert (
        repository.get(
            record.asset_class, record.data_type, record.symbol, record.session_date
        )
        == record
    )


@pytest.mark.parametrize("state", list(JobState))
def test_job_state_and_utc_timestamps_round_trip_through_entity(
    state: JobState,
) -> None:
    record = _job(state)
    entity = job_to_entity(record)

    assert entity["PartitionKey"] == "jobs"
    assert entity["RowKey"] == "job%2F2024-01-19"
    assert job_from_entity(entity) == record


@pytest.mark.parametrize("state", list(JobState))
def test_job_state_round_trips_through_memory(state: JobState) -> None:
    record = _job(state)
    repository = InMemoryJobRepository()

    repository.upsert(record)

    assert repository.get(record.job_id) == record


def test_contract_decimal_and_lifecycle_dates_round_trip_through_entity() -> None:
    record = _contract()
    entity = contract_to_entity(record)

    assert entity["StrikePrice"] == "150.125000000000000001"
    assert contract_from_entity(entity) == record


def test_memory_repositories_upsert_filter_and_sort_deterministically() -> None:
    first = _coverage(CoverageStatus.PARTIAL)
    second = CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="MSFT",
        session_date=date(2024, 1, 18),
        status=CoverageStatus.EMPTY,
    )
    catalog: CatalogRepository = InMemoryCatalogRepository([first, second])
    jobs: JobRepository = InMemoryJobRepository([_job()])
    contracts: ContractRepository = InMemoryContractRepository([_contract()])

    assert (
        catalog.get(AssetClass.EQUITY, DataType.TRADE, "AAPL", first.session_date)
        == first
    )
    assert [item.symbol for item in catalog.list()] == ["AAPL", "MSFT"]
    assert catalog.upsert(_coverage()) == _coverage()
    assert len(catalog.list(symbol="aapl")) == 1
    assert jobs.get("job/2024-01-19") == _job()
    assert jobs.list(state=JobState.QUEUED) == (_job(),)
    assert contracts.get("AAPL240119C00150000") == _contract()
    assert contracts.list(underlying="AAPL", as_of=date(2024, 1, 19)) == (_contract(),)


@pytest.mark.parametrize(
    ("repository", "record", "expected_key"),
    [
        (
            AzureTableCatalogRepository,
            _coverage(CoverageStatus.ERROR),
            ("coverage|equity|trade|AAPL", "2024-01-19"),
        ),
        (
            AzureTableJobRepository,
            _job(JobState.FAILED),
            ("jobs", "job%2F2024-01-19"),
        ),
        (
            AzureTableContractRepository,
            _contract(),
            ("contracts", "AAPL240119C00150000"),
        ),
    ],
)
def test_azure_adapters_round_trip_without_live_azure(
    repository: type[Any], record: Any, expected_key: tuple[str, str]
) -> None:
    table = FakeTable()
    adapter = repository(cast(TableClient, table))

    assert adapter.upsert(record) == record
    assert expected_key in table.entities
    if isinstance(record, ContractRecord):
        assert adapter.get(record.symbol) == record
    elif isinstance(record, JobRecord):
        assert adapter.get(record.job_id) == record
    else:
        assert (
            adapter.get(
                record.asset_class, record.data_type, record.symbol, record.session_date
            )
            == record
        )


def test_azure_catalog_uses_partition_query_and_deterministic_date_filter() -> None:
    table = FakeTable()
    repository = AzureTableCatalogRepository(cast(TableClient, table))
    repository.upsert(_coverage())

    records = repository.list(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
    )

    assert records == (_coverage(),)
    assert table.queries == [
        (
            "PartitionKey eq @partition",
            {"partition": "coverage|equity|trade|AAPL"},
        )
    ]


def test_azure_job_and_contract_lists_filter_after_table_scan() -> None:
    table = FakeTable()
    jobs = AzureTableJobRepository(cast(TableClient, table))
    contracts = AzureTableContractRepository(cast(TableClient, table))
    jobs.upsert(_job(JobState.COMPLETE))
    contracts.upsert(_contract())

    assert jobs.list(state=JobState.COMPLETE) == (_job(JobState.COMPLETE),)
    assert contracts.list(underlying="AAPL", as_of=date(2024, 1, 19)) == (_contract(),)


def test_reversed_catalog_date_ranges_are_rejected() -> None:
    with pytest.raises(ValueError, match="start must be on or before end"):
        InMemoryCatalogRepository().list(start=date(2024, 1, 2), end=date(2024, 1, 1))
