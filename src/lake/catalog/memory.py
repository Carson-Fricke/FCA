"""Deterministic in-memory catalog adapters."""

from collections.abc import Iterable
from datetime import date

from lake.catalog.models import (
    ContractRecord,
    CoverageRecord,
    JobRecord,
    JobState,
    ListingRecord,
    RunRecord,
    RunState,
    canonical_symbol,
)
from lake.contracts import AssetClass, DataType


class InMemoryCatalogRepository:
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
            canonical_symbol(symbol),
            session_date,
        )
        return self._records.get(key)

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        self._records[record.key] = record
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
        asset_class = AssetClass(asset_class) if asset_class is not None else None
        data_type = DataType(data_type) if data_type is not None else None
        symbol = canonical_symbol(symbol) if symbol is not None else None
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


class InMemoryRunRepository:
    def __init__(self, records: Iterable[RunRecord] = ()) -> None:
        self._records = {record.job_id: record for record in records}

    def get(self, job_id: str) -> RunRecord | None:
        return self._records.get(job_id.strip())

    def upsert(self, record: RunRecord) -> RunRecord:
        self._records[record.job_id] = record
        return record

    def put(self, record: RunRecord) -> RunRecord:
        return self.upsert(record)

    def list(
        self,
        *,
        status: RunState | None = None,
        strategy_id: str | None = None,
    ) -> tuple[RunRecord, ...]:
        status = RunState(status) if status is not None else None
        strategy_id = strategy_id.strip() if strategy_id is not None else None
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.job_id)
            if (status is None or record.status == status)
            and (strategy_id is None or record.strategy_id == strategy_id)
        )

    def delete(self, job_id: str) -> bool:
        return self._records.pop(job_id.strip(), None) is not None


class InMemoryContractRepository:
    def __init__(self, records: Iterable[ContractRecord] = ()) -> None:
        self._records = {record.symbol: record for record in records}

    def get(self, symbol: str) -> ContractRecord | None:
        return self._records.get(canonical_symbol(symbol))

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
        underlying = canonical_symbol(underlying) if underlying is not None else None
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


class InMemoryListingRepository:
    """Deterministic process-local storage for security listing episodes."""

    def __init__(self, records: Iterable[ListingRecord] = ()) -> None:
        self._records = {record.key: record for record in records}

    def get(self, entity_id: str, symbol: str) -> ListingRecord | None:
        return self._records.get(
            (_canonical_entity(entity_id), canonical_symbol(symbol))
        )

    def upsert(self, record: ListingRecord) -> ListingRecord:
        self._records[record.key] = record
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
        entity_id = _canonical_entity(entity_id) if entity_id is not None else None
        symbol = canonical_symbol(symbol) if symbol is not None else None
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.key)
            if (entity_id is None or record.entity_id == entity_id)
            and (symbol is None or record.symbol == symbol)
            and (as_of is None or record.is_known(as_of))
        )


def _canonical_entity(entity_id: str) -> str:
    canonical = entity_id.strip().upper()
    if not canonical:
        raise ValueError("entity_id must not be empty")
    return canonical


__all__ = [
    "InMemoryCatalogRepository",
    "InMemoryContractRepository",
    "InMemoryJobRepository",
    "InMemoryListingRepository",
    "InMemoryRunRepository",
]
