"""Persistence ports for catalog capabilities."""

from datetime import date
from typing import Protocol

from lake.catalog.models import ContractRecord, CoverageRecord, JobRecord, JobState
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


__all__ = ["CatalogRepository", "ContractRepository", "JobRepository"]
