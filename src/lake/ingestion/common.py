"""Shared ports and value objects for atomic asset ingestion."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from lake.catalog.models import CoverageRecord
from lake.contracts import AssetClass, DataType
from lake.storage.parquet import ObjectStore, ParquetMetadata


@dataclass(frozen=True, slots=True)
class StagedParquetObject:
    """A complete Parquet payload waiting for an atomic ingest commit."""

    metadata: ParquetMetadata
    payload: bytes


@runtime_checkable
class AtomicIngestCommitter(Protocol):
    """Publish objects and coverage rows as one atomic unit."""

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None: ...


class PartitionWriter(Protocol):
    def write(
        self,
        records: Sequence[Mapping[str, object]],
        *,
        session_date: date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
    ) -> ParquetMetadata: ...


WriterFactory = Callable[[ObjectStore], PartitionWriter]

__all__ = [
    "AtomicIngestCommitter",
    "PartitionWriter",
    "StagedParquetObject",
    "WriterFactory",
]
