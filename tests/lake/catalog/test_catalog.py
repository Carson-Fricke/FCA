from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from lake.catalog import (
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
)
from lake.contracts import AssetClass, DataType


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
def test_job_state_round_trips_through_memory(state: JobState) -> None:
    record = _job(state)
    repository = InMemoryJobRepository()

    repository.upsert(record)

    assert repository.get(record.job_id) == record


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


def test_reversed_catalog_date_ranges_are_rejected() -> None:
    with pytest.raises(ValueError, match="start must be on or before end"):
        InMemoryCatalogRepository().list(start=date(2024, 1, 2), end=date(2024, 1, 1))
