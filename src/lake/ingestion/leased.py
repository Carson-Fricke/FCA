"""Lease-protected atomic ingest composition."""

from collections.abc import Sequence

from lake.catalog.models import CoverageRecord
from lake.ingestion.common import AtomicIngestCommitter, StagedParquetObject
from lake.storage.leases import LeaseProvider, SymbolDayScope, hold_lease


def scope_for_coverage(coverage: Sequence[CoverageRecord]) -> SymbolDayScope:
    """Derive and validate the one symbol-day protected by an atomic commit."""
    records = tuple(coverage)
    if not records:
        raise ValueError("a leased atomic commit requires coverage records")
    first = records[0]
    expected = first.asset_class, first.symbol, first.session_date
    if any(
        (record.asset_class, record.symbol, record.session_date) != expected
        for record in records[1:]
    ):
        raise ValueError("a leased atomic commit must contain one symbol-day")
    return SymbolDayScope(*expected)


class LeasedAtomicIngestCommitter:
    """Protect an atomic object/catalog commit with one symbol-day lease."""

    def __init__(
        self,
        committer: AtomicIngestCommitter,
        leases: LeaseProvider,
    ) -> None:
        self._committer = committer
        self._leases = leases

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        scope = scope_for_coverage(coverage)
        with hold_lease(self._leases, scope):
            self._committer.commit(objects=objects, coverage=coverage)


__all__ = ["LeasedAtomicIngestCommitter", "scope_for_coverage"]
