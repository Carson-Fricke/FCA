"""Alpaca ingestion composed over local filesystem adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from lake.catalog.models import CoverageRecord, CoverageStatus, canonical_symbol
from lake.configuration import load_settings, load_universes
from lake.contracts import symbol_filename
from lake.coverage import CoverageSession, crypto_sessions, equity_sessions
from lake.ingestion.common import StagedParquetObject
from lake.ingestion.crypto.transaction import CryptoIngestTransaction
from lake.ingestion.equities.transaction import EquityIngestTransaction
from lake.local.state import LocalCatalogRepository, LocalLayout
from lake.providers.alpaca.client import AlpacaClient
from lake.providers.alpaca.records import (
    NormalizedCryptoQuote,
    NormalizedCryptoTrade,
    NormalizedQuote,
    NormalizedTrade,
)
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
    ) -> Sequence[NormalizedTrade]: ...

    async def fetch_quotes(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedQuote]: ...


class AlpacaCryptoProvider(Protocol):
    """Crypto Alpaca surface kept injectable for fake-only tests."""

    async def fetch_crypto_trades(
        self,
        pairs: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoTrade]: ...

    async def fetch_crypto_quotes(
        self,
        pairs: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoQuote]: ...


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


class _EquityProviderAdapter:
    """Expose the transaction's ``get_*`` seam over the provider ``fetch_*`` seam.

    The adapter applies no record bound: pagination and page sizing are the
    Alpaca client's internal policy, so callers only ever request a window.
    """

    def __init__(self, provider: AlpacaEquityProvider) -> None:
        self._provider = provider

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        return await self._provider.fetch_trades(symbol, start, end)

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedQuote]:
        return await self._provider.fetch_quotes(symbol, start, end)


class _CryptoProviderAdapter:
    """Expose the transaction's crypto seam without leaking a record limit."""

    def __init__(self, provider: AlpacaCryptoProvider) -> None:
        self._provider = provider

    async def get_crypto_trades(
        self,
        pair: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoTrade]:
        return await self._provider.fetch_crypto_trades(pair, start, end)

    async def get_crypto_quotes(
        self,
        pair: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoQuote]:
        return await self._provider.fetch_crypto_quotes(pair, start, end)


async def run_local_ingest(
    *,
    layout: LocalLayout,
    symbol: str | None = None,
    session_date: date | None = None,
    provider: AlpacaEquityProvider | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Fetch one closed equity session from Alpaca into deterministic local state."""
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
    current_time = datetime.now(UTC) if now is None else _utc(now)
    session = _select_session(session_date, current_time)
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
            client=_EquityProviderAdapter(alpaca),
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


async def run_local_backfill(
    *,
    layout: LocalLayout,
    symbols: Sequence[str],
    start_date: date,
    end_date: date,
    provider: AlpacaEquityProvider | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Fetch every closed equity session an arbitrary symbol set still misses.

    Unlike :func:`run_local_ingest`, the symbol set is not restricted to the
    configured soak universe: any canonical equity symbol may be requested.
    Sessions with terminal coverage for every requested data type are skipped,
    so a repeated ensure resumes from the catalog instead of refetching.
    """
    normalized = _normalize_symbols(symbols)
    if end_date < start_date:
        raise ValueError("start_date must be on or before end_date")
    current_time = datetime.now(UTC) if now is None else _utc(now)
    settings = load_settings(environ=environ)
    if settings.provider.api_key is None or settings.provider.api_secret is None:
        raise LocalIngestError(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required "
            "for local backfill"
        )

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

    sessions = equity_sessions(start_date, end_date, as_of=current_time)
    complete = _terminal_keys(catalog, start_date, end_date)
    fetched: list[dict[str, object]] = []
    failures: list[str] = []
    try:
        for symbol in normalized:
            for session in sessions:
                if _session_is_terminal(complete, symbol, session.session_date):
                    continue
                result = await EquityIngestTransaction(
                    client=_EquityProviderAdapter(alpaca),
                    committer=committer,
                ).run(symbol, session)
                _remember(complete, result.coverage)
                fetched.append(
                    {
                        "data_type_rows": {
                            record.data_type.value: record.row_count
                            for record in result.coverage
                        },
                        "session_date": result.session_date.isoformat(),
                        "statuses": [record.status.value for record in result.coverage],
                        "symbol": result.symbol,
                    }
                )
                failures.extend(
                    record.error_message or "provider request failed"
                    for record in result.coverage
                    if record.status is CoverageStatus.ERROR
                )
    finally:
        if owned_provider is not None:
            await owned_provider.aclose()

    if failures:
        raise LocalIngestError(f"Alpaca backfill failed: {failures[0]}")
    return {
        "data_root": str(layout.root),
        "requested_end": end_date.isoformat(),
        "requested_start": start_date.isoformat(),
        "sessions": fetched,
        "symbols": list(normalized),
    }


async def run_local_crypto_backfill(
    *,
    layout: LocalLayout,
    pairs: Sequence[str],
    start_date: date,
    end_date: date,
    provider: AlpacaCryptoProvider | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Fetch every UTC day a crypto pair set still misses from Alpaca."""
    normalized = _normalize_pairs(pairs)
    if end_date < start_date:
        raise ValueError("start_date must be on or before end_date")
    current_time = datetime.now(UTC) if now is None else _utc(now)
    settings = load_settings(environ=environ)
    if settings.provider.api_key is None or settings.provider.api_secret is None:
        raise LocalIngestError(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required "
            "for local backfill"
        )

    layout.initialize()
    catalog = LocalCatalogRepository(layout.catalog)
    committer = LocalIngestCommitter(
        object_store=LocalFilesystemStore(layout.objects),
        catalog=catalog,
    )
    owned_provider: AlpacaClient | None = None
    if provider is None:
        owned_provider = AlpacaClient.from_settings(settings.provider)
        alpaca: AlpacaCryptoProvider = owned_provider
    else:
        alpaca = provider

    sessions = crypto_sessions(start_date, end_date, as_of=current_time)
    complete = _terminal_keys(catalog, start_date, end_date)
    fetched: list[dict[str, object]] = []
    failures: list[str] = []
    try:
        for pair in normalized:
            for session in sessions:
                if _session_is_terminal(complete, pair, session.session_date):
                    continue
                result = await CryptoIngestTransaction(
                    client=_CryptoProviderAdapter(alpaca),
                    committer=committer,
                ).run(pair, session)
                _remember(complete, result.coverage)
                fetched.append(
                    {
                        "data_type_rows": {
                            record.data_type.value: record.row_count
                            for record in result.coverage
                        },
                        "session_date": result.session_date.isoformat(),
                        "statuses": [record.status.value for record in result.coverage],
                        "symbol": result.symbol,
                    }
                )
                failures.extend(
                    record.error_message or "provider request failed"
                    for record in result.coverage
                    if record.status is CoverageStatus.ERROR
                )
    finally:
        if owned_provider is not None:
            await owned_provider.aclose()

    if failures:
        raise LocalIngestError(f"Alpaca backfill failed: {failures[0]}")
    return {
        "asset_class": "crypto",
        "data_root": str(layout.root),
        "requested_end": end_date.isoformat(),
        "requested_start": start_date.isoformat(),
        "sessions": fetched,
        "symbols": list(normalized),
    }


def _normalize_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    if isinstance(symbols, str):
        raise TypeError("symbols must be a sequence of symbols")
    normalized: list[str] = []
    for value in symbols:
        if not isinstance(value, str):
            raise TypeError("symbols must contain strings")
        canonical = canonical_symbol(value)
        symbol_filename(canonical)
        if canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        raise ValueError("backfill requires at least one symbol")
    return tuple(normalized)


def _normalize_pairs(pairs: Iterable[str]) -> tuple[str, ...]:
    if isinstance(pairs, str):
        raise TypeError("pairs must be a sequence of pairs")
    normalized: list[str] = []
    for value in pairs:
        if not isinstance(value, str):
            raise TypeError("pairs must contain strings")
        canonical = value.strip().upper()
        if "/" not in canonical:
            raise ValueError(f"invalid crypto pair: {value!r}")
        symbol_filename(canonical)
        if canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        raise ValueError("backfill requires at least one crypto pair")
    return tuple(normalized)


def _terminal_keys(
    catalog: LocalCatalogRepository, start: date, end: date
) -> dict[tuple[str, date, object], CoverageStatus]:
    return {
        (record.symbol, record.session_date, record.data_type): record.status
        for record in catalog.list(start=start, end=end)
    }


def _session_is_terminal(
    keys: Mapping[tuple[str, date, object], CoverageStatus],
    symbol: str,
    session_date: date,
) -> bool:
    for data_type in ("trade", "quote"):
        status = keys.get((symbol, session_date, data_type))
        if status not in {CoverageStatus.COMPLETE, CoverageStatus.EMPTY}:
            return False
    return True


def _remember(
    keys: dict[tuple[str, date, object], CoverageStatus],
    coverage: Sequence[CoverageRecord],
) -> None:
    for record in coverage:
        keys[(record.symbol, record.session_date, record.data_type)] = record.status


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
    "AlpacaCryptoProvider",
    "AlpacaEquityProvider",
    "LocalIngestCommitter",
    "LocalIngestError",
    "run_local_backfill",
    "run_local_crypto_backfill",
    "run_local_ingest",
]
