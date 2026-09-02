"""Bounded Alpaca ingestion composed over local filesystem adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.configuration import load_settings, load_universes
from lake.coverage import CoverageSession, equity_sessions
from lake.ingestion.common import StagedParquetObject
from lake.ingestion.equities.transaction import EquityIngestTransaction
from lake.local.state import LocalCatalogRepository, LocalLayout
from lake.providers.alpaca.client import AlpacaClient
from lake.providers.alpaca.records import NormalizedQuote, NormalizedTrade
from lake.storage.parquet import LocalFilesystemStore

_DEFAULT_LOOKBACK_DAYS = 31


class LocalIngestError(RuntimeError):
    """A supported local ingest could not be started or completed."""


class AlpacaEquityProvider(Protocol):
    """Bounded Alpaca surface kept injectable for fake-only tests."""

    async def fetch_trades(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        max_records: int | None = None,
    ) -> Sequence[NormalizedTrade]: ...

    async def fetch_quotes(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        max_records: int | None = None,
    ) -> Sequence[NormalizedQuote]: ...


class LocalIngestCommitter:
    """Publish local objects before atomically replacing catalog JSON."""

    def __init__(
        self,
        *,
        object_store: LocalFilesystemStore,
        catalog: LocalCatalogRepository,
    ) -> None:
        self._object_store = object_store
        self._catalog = catalog

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        for staged in objects:
            self._object_store.replace(staged.metadata.path, staged.payload)
        self._catalog.upsert_many(coverage)


class _BoundedProvider:
    def __init__(self, provider: AlpacaEquityProvider, max_records: int) -> None:
        self._provider = provider
        self._max_records = max_records

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        return await self._provider.fetch_trades(
            symbol, start, end, max_records=self._max_records
        )

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedQuote]:
        return await self._provider.fetch_quotes(
            symbol, start, end, max_records=self._max_records
        )


async def run_local_ingest(
    *,
    layout: LocalLayout,
    symbol: str | None = None,
    session_date: date | None = None,
    max_records: int = 100,
    provider: AlpacaEquityProvider | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Fetch one closed equity session from Alpaca into deterministic local state."""
    if isinstance(max_records, bool) or not 1 <= max_records <= 10_000:
        raise ValueError("max_records must be between 1 and 10000")
    settings = load_settings(environ=environ)
    universes = load_universes(environ=environ)
    if settings.provider.api_key is None or settings.provider.api_secret is None:
        raise LocalIngestError(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required "
            "for local ingest"
        )

    selected_symbol = (
        universes.equities.soak.symbols[0] if symbol is None else symbol.strip().upper()
    )
    if selected_symbol not in universes.equities.soak.symbols:
        raise ValueError(
            f"symbol is not in the configured equity universe: {selected_symbol}"
        )
    current_time = datetime.now(UTC) if now is None else _utc(now)
    # A record bound can truncate either dataset, so bounded ingests are partial.
    session = replace(_select_session(session_date, current_time), is_partial=True)
    layout.initialize()
    catalog = LocalCatalogRepository(layout.catalog)
    committer = LocalIngestCommitter(
        object_store=LocalFilesystemStore(layout.objects),
        catalog=catalog,
    )
    owned_provider: AlpacaClient | None = None
    if provider is None:
        owned_provider = AlpacaClient.from_settings(settings.provider)
        alpaca: AlpacaEquityProvider = owned_provider
    else:
        alpaca = provider
    try:
        result = await EquityIngestTransaction(
            client=_BoundedProvider(alpaca, max_records),
            committer=committer,
        ).run(selected_symbol, session)
    finally:
        if owned_provider is not None:
            await owned_provider.aclose()

    failed = [
        record for record in result.coverage if record.status is CoverageStatus.ERROR
    ]
    if failed:
        detail = failed[0].error_message or "provider request failed"
        raise LocalIngestError(f"Alpaca ingest failed: {detail}")
    return {
        "bounded_records_per_data_type": max_records,
        "catalog": str(layout.catalog),
        "coverage": [
            {
                "data_type": record.data_type.value,
                "rows": record.row_count,
                "status": record.status.value,
            }
            for record in result.coverage
        ],
        "data_root": str(layout.root),
        "objects": [
            {"bytes": item.bytes, "path": item.path, "rows": item.rows}
            for item in result.objects
        ],
        "session_date": result.session_date.isoformat(),
        "symbol": result.symbol,
    }


def _select_session(session_date: date | None, now: datetime) -> CoverageSession:
    if session_date is None:
        sessions = equity_sessions(
            now.date() - timedelta(days=_DEFAULT_LOOKBACK_DAYS),
            now.date(),
            as_of=now,
        )
        closed = [session for session in sessions if session.close_time <= now]
        if not closed:
            raise LocalIngestError("no closed XNYS session is available")
        return max(closed, key=lambda session: session.session_date)

    sessions = equity_sessions(session_date, session_date, as_of=now)
    if not sessions or sessions[0].close_time > now:
        raise ValueError("session-date must identify a closed XNYS session")
    return sessions[0]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AlpacaEquityProvider",
    "LocalIngestCommitter",
    "LocalIngestError",
    "run_local_ingest",
]
