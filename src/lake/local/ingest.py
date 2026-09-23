"""Alpaca ingestion composed over local filesystem adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from lake.catalog.models import CoverageRecord, CoverageStatus, canonical_symbol
from lake.configuration import load_settings, load_universes
from lake.contracts import AssetClass, DataType, symbol_filename
from lake.coverage import CoverageSession, crypto_sessions, equity_sessions
from lake.ingestion.common import (
    StagedParquetObject,
    is_transient_fetch_error,
    normalize_data_types,
)
from lake.ingestion.crypto.transaction import (
    CryptoIngestResult,
    CryptoIngestTransaction,
)
from lake.ingestion.equities.transaction import (
    EquityIngestResult,
    EquityIngestTransaction,
)
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

#: Bounded concurrent per-session fetches. Each session already runs its trade
#: and quote fetches concurrently, and the Alpaca client bounds HTTP
#: concurrency and honors 429 Retry-After internally; the session-level bound
#: keeps overall request pressure inside Alpaca's 10k-per-minute budget.
_BACKFILL_CONCURRENCY = 4

#: Coverage rows buffered in the catalog before a flush. Objects are written
#: immediately, but the catalog rewrite (read + fsync + atomic replace of the
#: whole file) is batched: durability after a crash is traded for throughput,
#: and coverage-driven resume re-fetches any session whose rows were never
#: flushed, so an interrupted backfill is never published as complete.
_CATALOG_FLUSH_RECORDS = 64

#: Settle delay before the one in-ensure retry pass over transiently-failed
#: sessions: a transport hiccup (DNS/connect) gets one more attempt once the
#: walk has finished, instead of leaving the recovery to the next run.
_RETRY_SETTLE_SECONDS = 2.0


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


class _BackfillCommitter(LocalIngestCommitter):
    """Publish objects per session while batching the catalog persistence.

    Parquet objects are written through on every commit; only the catalog
    rewrite (full read + fsync + atomic replace of the whole file) is buffered
    and flushed in batches.  A crash before a flush loses catalog rows but not
    objects: coverage-driven resume re-fetches those uncommitted sessions
    deterministically, so an interrupted backfill is never published as
    complete and no compensating cleanup is required.
    """

    def __init__(
        self,
        *,
        object_store: LocalFilesystemStore,
        catalog: LocalCatalogRepository,
        flush_records: int,
    ) -> None:
        super().__init__(object_store=object_store, catalog=catalog)
        self._flush_records = flush_records
        self._pending: dict[tuple[AssetClass, DataType, str, date], CoverageRecord] = {}

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        for staged in objects:
            self._object_store.replace(staged.metadata.path, staged.payload)
        for record in coverage:
            self._pending[record.key] = record
        if len(self._pending) >= self._flush_records:
            self.flush()

    def flush(self) -> None:
        """Persist every buffered coverage row in one catalog update.

        Rows are keyed by their logical coverage key: a retry that replaces an
        earlier error row with a complete one overwrites it in place, so the
        single flushed update never carries duplicate keys.
        """
        if not self._pending:
            return
        self._catalog.upsert_many(tuple(self._pending.values()))
        self._pending.clear()


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
    data_types: Sequence[DataType | str] | DataType | str | None = None,
) -> dict[str, object]:
    """Fetch every closed equity session an arbitrary symbol set still misses.

    Unlike :func:`run_local_ingest`, the symbol set is not restricted to the
    configured soak universe: any canonical equity symbol may be requested.
    Sessions with terminal coverage for every requested data type are skipped,
    so a repeated ensure resumes from the catalog instead of refetching.

    ``data_types`` narrows the work to the requested datasets. One ensure
    request asks for one data type, and only that dataset is fetched: two
    ensures for the same symbol no longer each fetch the other's data.

    Sessions are fetched concurrently within a bounded request budget. The
    backfill completes every fetchable session: per-session failures are
    recorded as error coverage and surfaced in the result's ``sessions`` and
    ``errors`` entries instead of failing the whole range. The function raises
    only when nothing could be processed, leaving the caller (the harness) to
    decide whether surfaced errors constitute a study failure.
    """
    normalized = _normalize_symbols(symbols)
    selected = normalize_data_types(data_types)
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
    committer = _BackfillCommitter(
        object_store=LocalFilesystemStore(layout.objects),
        catalog=catalog,
        flush_records=_CATALOG_FLUSH_RECORDS,
    )
    owned_provider: AlpacaClient | None = None
    if provider is None:
        owned_provider = AlpacaClient.from_settings(settings.provider)
        alpaca: AlpacaEquityProvider = owned_provider
    else:
        alpaca = provider

    sessions = equity_sessions(start_date, end_date, as_of=current_time)
    complete = _terminal_keys(catalog, start_date, end_date)
    planned = [
        (symbol, session)
        for symbol in normalized
        for session in sessions
        if not _session_is_terminal(complete, symbol, session.session_date, selected)
    ]

    def ingest(symbol: str, session: CoverageSession) -> Awaitable[EquityIngestResult]:
        return EquityIngestTransaction(
            client=_EquityProviderAdapter(alpaca),
            committer=committer,
        ).run(symbol, session, data_types=selected)

    succeeded = 0

    def record(
        result: object, symbol: str, session: CoverageSession
    ) -> dict[str, object]:
        nonlocal succeeded
        if isinstance(result, asyncio.CancelledError):
            raise result
        if not isinstance(result, (EquityIngestResult, CryptoIngestResult)):
            return _failed_entry(result, symbol, session)
        entry, failed = _result_entry(result)
        if failed is None:
            succeeded += 1
            _remember(complete, result.coverage)
        return entry

    try:
        fetched = await _run_with_transient_retry(planned, ingest, record)
    finally:
        if owned_provider is not None:
            await owned_provider.aclose()
    committer.flush()

    failures = [entry for entry in fetched if entry["error"] is not None]
    if failures and succeeded == 0:
        raise LocalIngestError(f"Alpaca backfill failed: {failures[0]['error']}")
    return {
        "data_root": str(layout.root),
        "errors": failures,
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
    data_types: Sequence[DataType | str] | DataType | str | None = None,
) -> dict[str, object]:
    """Fetch every UTC day a crypto pair set still misses from Alpaca.

    Like the equity backfill, pair-days are fetched concurrently within a
    bounded request budget, fetchable days always complete, and per-day
    failures are recorded as error coverage and surfaced in the result instead
    of failing the whole range. The function raises only when nothing could be
    processed. ``data_types`` narrows the work to the requested datasets.
    """
    normalized = _normalize_pairs(pairs)
    selected = normalize_data_types(data_types)
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
    committer = _BackfillCommitter(
        object_store=LocalFilesystemStore(layout.objects),
        catalog=catalog,
        flush_records=_CATALOG_FLUSH_RECORDS,
    )
    owned_provider: AlpacaClient | None = None
    if provider is None:
        owned_provider = AlpacaClient.from_settings(settings.provider)
        alpaca: AlpacaCryptoProvider = owned_provider
    else:
        alpaca = provider

    sessions = crypto_sessions(start_date, end_date, as_of=current_time)
    complete = _terminal_keys(catalog, start_date, end_date)
    planned = [
        (pair, session)
        for pair in normalized
        for session in sessions
        if not _session_is_terminal(complete, pair, session.session_date, selected)
    ]

    def ingest(pair: str, session: CoverageSession) -> Awaitable[CryptoIngestResult]:
        return CryptoIngestTransaction(
            client=_CryptoProviderAdapter(alpaca),
            committer=committer,
        ).run(pair, session, data_types=selected)

    succeeded = 0

    def record(
        result: object, symbol: str, session: CoverageSession
    ) -> dict[str, object]:
        nonlocal succeeded
        if isinstance(result, asyncio.CancelledError):
            raise result
        if not isinstance(result, (EquityIngestResult, CryptoIngestResult)):
            return _failed_entry(result, symbol, session)
        entry, failed = _result_entry(result)
        if failed is None:
            succeeded += 1
            _remember(complete, result.coverage)
        return entry

    try:
        fetched = await _run_with_transient_retry(planned, ingest, record)
    finally:
        if owned_provider is not None:
            await owned_provider.aclose()
    committer.flush()

    failures = [entry for entry in fetched if entry["error"] is not None]
    if failures and succeeded == 0:
        raise LocalIngestError(f"Alpaca backfill failed: {failures[0]['error']}")
    return {
        "asset_class": "crypto",
        "data_root": str(layout.root),
        "errors": failures,
        "requested_end": end_date.isoformat(),
        "requested_start": start_date.isoformat(),
        "sessions": fetched,
        "symbols": list(normalized),
    }


async def _run_planned(
    planned: Sequence[tuple[str, CoverageSession]],
    ingest: Callable[[str, CoverageSession], Awaitable[object]],
) -> list[object]:
    """Run every planned session concurrently within the bounded budget.

    ``asyncio.gather`` preserves the planned order in its results regardless
    of completion order, so the backfill result stays deterministic.
    """
    semaphore = asyncio.Semaphore(_BACKFILL_CONCURRENCY)

    async def bounded(symbol: str, session: CoverageSession) -> object:
        async with semaphore:
            return await ingest(symbol, session)

    return list(
        await asyncio.gather(
            *(bounded(symbol, session) for symbol, session in planned),
            return_exceptions=True,
        )
    )


async def _run_with_transient_retry(
    planned: Sequence[tuple[str, CoverageSession]],
    ingest: Callable[[str, CoverageSession], Awaitable[object]],
    record: Callable[[object, str, CoverageSession], dict[str, object]],
) -> list[dict[str, object]]:
    """Run every planned session, retrying transiently-failed ones in-ensure.

    Session failures surface as error entries as the walk proceeds; sessions
    whose failure is classified retryable (transport-level fetch errors) get
    one settle-delayed second attempt before the failures reach the caller,
    so a DNS/connect hiccup heals inside this run instead of the next one.
    Permanent failures are never re-run: they fail identically forever.
    """
    entries = [
        record(result, symbol, session)
        for (symbol, session), result in zip(
            planned, await _run_planned(planned, ingest), strict=True
        )
    ]
    retryable = [
        index
        for index, entry in enumerate(entries)
        if entry["error"] is not None and entry["retryable"]
    ]
    if not retryable:
        return entries
    await asyncio.sleep(_RETRY_SETTLE_SECONDS)
    plan = [planned[index] for index in retryable]
    for index, (symbol, session), result in zip(
        retryable,
        plan,
        await _run_planned(plan, ingest),
        strict=True,
    ):
        entries[index] = record(result, symbol, session)
    return entries


def _result_entry(
    result: EquityIngestResult | CryptoIngestResult,
) -> tuple[dict[str, object], str | None]:
    """Build one result entry, surfacing error coverage as the session error."""
    errors = [
        record.error_message or "provider request failed"
        for record in result.coverage
        if record.status is CoverageStatus.ERROR
    ]
    failed = errors[0] if errors else None
    entry = {
        "data_type_rows": {
            record.data_type.value: record.row_count for record in result.coverage
        },
        "error": failed,
        "retryable": result.retryable,
        "session_date": result.session_date.isoformat(),
        "statuses": [record.status.value for record in result.coverage],
        "symbol": result.symbol,
    }
    return entry, failed


def _failed_entry(
    error: object,
    symbol: str,
    session: CoverageSession,
) -> dict[str, object]:
    """Build one entry for a session whose transaction raised.

    The transaction deliberately propagates failed commits without a
    compensating write, so the session is reported as failed without coverage
    and the next ensure retries it.
    """
    if isinstance(error, BaseException):
        detail = str(error).strip()
        message = (
            type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"
        )
        retryable = is_transient_fetch_error(error)
    else:
        message = f"unexpected ingest result: {error!r}"
        retryable = False
    return {
        "data_type_rows": {},
        "error": message,
        "retryable": retryable,
        "session_date": session.session_date.isoformat(),
        "statuses": [],
        "symbol": symbol,
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
    data_types: Sequence[DataType],
) -> bool:
    """Whether every *requested* data type is terminal for one session.

    Only the requested datasets count: a trade-only backfill must not re-plan
    a session merely because its quotes are still missing.
    """
    for data_type in data_types:
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
