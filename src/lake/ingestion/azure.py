"""Azure composition for the provider-backed ingest transactions."""

from __future__ import annotations

from collections.abc import Sequence

from lake.catalog.models import CoverageRecord
from lake.catalog.ports import CatalogRepository
from lake.ingestion.common import StagedParquetObject
from lake.storage.ports import BlobStore


class AzureAtomicIngestCommitter:
    """Publish deterministic objects and their catalog rows to Azure."""

    def __init__(
        self,
        *,
        object_store: BlobStore,
        catalog: CatalogRepository,
    ) -> None:
        self._object_store = object_store
        self._catalog = catalog

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        """Replace all object payloads before publishing matching coverage rows."""
        for staged in objects:
            self._object_store.replace(staged.metadata.path, staged.payload)
        for record in coverage:
            self._catalog.upsert(record)


__all__ = ["AzureAtomicIngestCommitter"]
