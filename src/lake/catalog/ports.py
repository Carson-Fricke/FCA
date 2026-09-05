"""Persistence ports for catalog capabilities."""

from datetime import date
from typing import Protocol

from lake.catalog.models import (
    ContractRecord,
    CoverageRecord,
    JobRecord,
    JobState,
    ListingRecord,
    RunRecord,
    RunState,
)
from lake.contracts import AssetClass, DataType


class CatalogRepository(Protocol):
    """Persistence contract for symbol-day coverage metadata."""

    def get(
        self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
    ) -> CoverageRecord | None: ...

    def upsert(self, record: CoverageRecord) -> CoverageRecord: ...

    def list(
        self,
        *,
        asset_class: AssetClass | None = None,
        data_type: DataType | None = None,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CoverageRecord, ...]: ...


class JobRepository(Protocol):
    """Persistence contract for asynchronous job state."""

    def get(self, job_id: str) -> JobRecord | None: ...

    def upsert(self, record: JobRecord) -> JobRecord: ...

    def list(self, *, state: JobState | None = None) -> tuple[JobRecord, ...]: ...


class RunRepository(Protocol):
    """Persistence contract for stored backtest run metadata."""

    def get(self, run_id: str) -> RunRecord | None: ...

    def upsert(self, record: RunRecord) -> RunRecord: ...

    def list(
        self,
        *,
        status: RunState | None = None,
        strategy_id: str | None = None,
    ) -> tuple[RunRecord, ...]: ...

    def delete(self, run_id: str) -> bool: ...


class ContractRepository(Protocol):
    """Persistence contract for option contract metadata."""

    def get(self, symbol: str) -> ContractRecord | None: ...

    def upsert(self, record: ContractRecord) -> ContractRecord: ...

    def list(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ContractRecord, ...]: ...


class ListingRepository(Protocol):
    """Persistence contract for point-in-time security listing episodes."""

    def get(self, entity_id: str, symbol: str) -> ListingRecord | None: ...

    def upsert(self, record: ListingRecord) -> ListingRecord: ...

    def list(
        self,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ListingRecord, ...]: ...


__all__ = [
    "CatalogRepository",
    "ContractRepository",
    "JobRepository",
    "ListingRepository",
    "RunRepository",
]
