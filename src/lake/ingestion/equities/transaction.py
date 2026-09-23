"""Atomic, dependency-injected ingestion for one equity symbol session.

The existing Parquet and catalog protocols each represent one resource. This
module adds the transaction boundary needed to publish the requested data types
and their coverage rows as one commit supplied by the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol, cast

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.contracts import AssetClass, DataType, symbol_filename
from lake.coverage import CoverageSession
from lake.ingestion.common import (
    AtomicIngestCommitter,
    PartitionWriter,
    StagedParquetObject,
    WriterFactory,
    fetch_with_transient_retry,
    is_transient_fetch_error,
    normalize_data_types,
)
from lake.providers.alpaca.records import NormalizedQuote, NormalizedTrade
from lake.storage.capacity import StorageCapGuard
from lake.storage.parquet import ParquetMetadata, ParquetWriter


class EquityMarketDataClient(Protocol):
    """Provider boundary for the two T033 one-symbol fetches."""

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        """Fetch normalized trades for one symbol and session range."""

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedQuote]:
        """Fetch normalized quotes for one symbol and session range."""


@dataclass(frozen=True, slots=True)
class EquityIngestResult:
    """Published metadata for one symbol and session transaction."""

    symbol: str
    session_date: date
    coverage: tuple[CoverageRecord, ...]
    objects: tuple[ParquetMetadata, ...]
    #: Whether the session's fetch failure was a transport-level hiccup: the
    #: backfill may retry it in-ensure; permanent failures are never retried.
    retryable: bool = False


class EquityIngestTransaction:
    """Fetch, stage, and atomically publish one equity symbol session."""

    def __init__(
        self,
        *,
        client: EquityMarketDataClient,
        committer: AtomicIngestCommitter,
        writer_factory: WriterFactory = ParquetWriter,
        storage_guard: StorageCapGuard | None = None,
    ) -> None:
        self._client = client
        self._committer = committer
        self._writer_factory = writer_factory
        self._storage_guard = storage_guard

    async def run(
        self,
        symbol: str,
        session: CoverageSession,
        *,
        data_types: Sequence[DataType | str] | DataType | str | None = None,
    ) -> EquityIngestResult:
        """Fetch and publish the requested data types for one planned session.

        Every provider request completes before staging begins. Fetch or
        normalization failures publish only error metadata through the same
        atomic boundary; a failed commit is propagated without a compensating
        write that could hide a partial backend failure.

        ``data_types`` selects the published datasets. A trade-only ensure
        fetches and publishes trades alone, so two ensure requests for the same
        symbol stop duplicating each other's whole session.
        """
        canonical_symbol = _canonical_symbol(symbol)
        selected = normalize_data_types(data_types)
        if self._storage_guard is not None:
            self._storage_guard.enforce_ingest()
        # Alpaca's ``end`` parameter is inclusive, so the fetch window is
        # [open_time, close_time] on both ends: a raw print stamped exactly at
        # the session close (the 16:00:00 ET closing auction print) is part of
        # this session's data and must be accepted by the record validation.
        results = list(
            await asyncio.gather(
                *(
                    fetch_with_transient_retry(
                        lambda data_type=data_type: self._fetch(
                            data_type, canonical_symbol, session
                        )
                    )
                    for data_type in selected
                ),
                return_exceptions=True,
            )
        )

        fetch_error = _fetch_error(selected, results)
        if fetch_error is not None:
            return self._publish_error(
                canonical_symbol,
                session,
                selected,
                fetch_error,
                retryable=_fetch_retryable(results),
            )

        records_by_type: dict[DataType, Sequence[object]] = {
            data_type: cast(Sequence[object], result)
            for data_type, result in zip(selected, results, strict=True)
        }
        try:
            staged, metadata = self._stage(
                canonical_symbol,
                session,
                selected,
                records_by_type,
            )
        except Exception as error:
            return self._publish_error(
                canonical_symbol,
                session,
                selected,
                _error_message(error),
            )

        if self._storage_guard is not None:
            self._storage_guard.enforce_ingest(
                additional_bytes=sum(item.metadata.bytes for item in staged)
            )

        coverage = tuple(
            _coverage_record(
                canonical_symbol,
                session,
                data_type,
                _coverage_status(session.is_partial, len(records_by_type[data_type])),
                metadata[data_type],
            )
            for data_type in selected
        )
        self._committer.commit(objects=staged, coverage=coverage)
        return EquityIngestResult(
            symbol=canonical_symbol,
            session_date=session.session_date,
            coverage=coverage,
            objects=tuple(metadata[data_type] for data_type in selected),
        )

    async def _fetch(
        self,
        data_type: DataType,
        symbol: str,
        session: CoverageSession,
    ) -> Sequence[NormalizedTrade] | Sequence[NormalizedQuote]:
        """Fetch one requested dataset for one symbol session."""
        if data_type is DataType.TRADE:
            return await self._client.get_trades(
                symbol, session.open_time, session.close_time
            )
        return await self._client.get_quotes(
            symbol, session.open_time, session.close_time
        )

    def _stage(
        self,
        symbol: str,
        session: CoverageSession,
        data_types: Sequence[DataType],
        records_by_type: Mapping[DataType, Sequence[object]],
    ) -> tuple[tuple[StagedParquetObject, ...], dict[DataType, ParquetMetadata]]:
        store = _StagingObjectStore()
        writer = self._writer_factory(store)
        metadata: dict[DataType, ParquetMetadata] = {}
        for data_type in data_types:
            records = records_by_type[data_type]
            mappings: Sequence[Mapping[str, object]]
            if data_type is DataType.TRADE:
                mappings = _trade_mappings(
                    cast(Sequence[NormalizedTrade], records), symbol, session
                )
            else:
                mappings = _quote_mappings(
                    cast(Sequence[NormalizedQuote], records), symbol, session
                )
            metadata[data_type] = writer.write(
                mappings,
                session_date=session.session_date,
                asset_class=AssetClass.EQUITY,
                data_type=data_type,
                symbol=symbol,
            )
        staged = tuple(
            StagedParquetObject(item, store.read(item.path))
            for item in metadata.values()
        )
        return staged, metadata

    def _publish_error(
        self,
        symbol: str,
        session: CoverageSession,
        data_types: Sequence[DataType],
        message: str,
        *,
        retryable: bool = False,
    ) -> EquityIngestResult:
        coverage = tuple(
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=data_type,
                symbol=symbol,
                session_date=session.session_date,
                status=CoverageStatus.ERROR,
                error_message=message,
            )
            for data_type in data_types
        )
        self._committer.commit(objects=(), coverage=coverage)
        return EquityIngestResult(
            symbol=symbol,
            session_date=session.session_date,
            coverage=coverage,
            objects=(),
            retryable=retryable,
        )


class _StagingObjectStore:
    """Private store that prevents staged bytes from being externally visible."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._objects[relative_path] = payload

    def read(self, relative_path: str) -> bytes:
        try:
            return self._objects[relative_path]
        except KeyError as error:
            raise RuntimeError(
                f"writer did not stage expected object {relative_path!r}"
            ) from error


def _canonical_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise TypeError("market symbol must be a string")
    canonical = symbol.strip().upper()
    symbol_filename(canonical)
    return canonical


def _fetch_error(
    data_types: Sequence[DataType], results: Sequence[object]
) -> str | None:
    errors: list[str] = []
    for data_type, result in zip(data_types, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            errors.append(f"{data_type.value}: {_error_message(result)}")
    return "; ".join(errors) if errors else None


def _fetch_retryable(results: Sequence[object]) -> bool:
    """Return whether every failing fetch failed transiently.

    A session whose fetches failed only at the transport level (DNS,
    connection, timeout) may be retried in-ensure; a session with a permanent
    failure re-runs into the same wall and must not be.
    """
    failures = [
        result
        for result in results
        if isinstance(result, BaseException)
        and not isinstance(result, asyncio.CancelledError)
    ]
    return bool(failures) and all(is_transient_fetch_error(e) for e in failures)


def _error_message(error: BaseException) -> str:
    detail = str(error).strip()
    return type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"


def _coverage_status(session_partial: bool, rows: int) -> CoverageStatus:
    """Return the coverage status for one data type.

    A closed session with rows is complete and a closed session with none is
    empty; only a session that is still trading is partial.
    """
    if session_partial:
        return CoverageStatus.PARTIAL
    return CoverageStatus.COMPLETE if rows else CoverageStatus.EMPTY


def _trade_mappings(
    records: Sequence[NormalizedTrade],
    symbol: str,
    session: CoverageSession,
) -> list[Mapping[str, object]]:
    ordered = sorted(
        (_validated_trade(record, symbol, session) for record in records),
        key=lambda record: (
            record.timestamp,
            record.trade_id,
            record.exchange,
            record.price,
            record.size,
            record.conditions,
            record.tape,
            record.update or "",
        ),
    )
    return [
        {
            "timestamp": record.timestamp,
            "symbol": record.symbol,
            "trade_id": record.trade_id,
            "exchange": record.exchange,
            "price": record.price,
            "size": record.size,
            "conditions": record.conditions,
            "tape": record.tape,
            "update": record.update,
        }
        for record in ordered
    ]


def _quote_mappings(
    records: Sequence[NormalizedQuote],
    symbol: str,
    session: CoverageSession,
) -> list[Mapping[str, object]]:
    ordered = sorted(
        (_validated_quote(record, symbol, session) for record in records),
        key=lambda record: (
            record.timestamp,
            record.bid_exchange,
            record.bid_price,
            record.bid_size,
            record.ask_exchange,
            record.ask_price,
            record.ask_size,
            record.conditions,
            record.tape,
        ),
    )
    return [
        {
            "timestamp": record.timestamp,
            "symbol": record.symbol,
            "bid_exchange": record.bid_exchange,
            "bid_price": record.bid_price,
            "bid_size": record.bid_size,
            "ask_exchange": record.ask_exchange,
            "ask_price": record.ask_price,
            "ask_size": record.ask_size,
            "conditions": record.conditions,
            "tape": record.tape,
        }
        for record in ordered
    ]


def _validated_trade(
    record: NormalizedTrade,
    symbol: str,
    session: CoverageSession,
) -> NormalizedTrade:
    _validate_record_timestamp(record.timestamp, symbol, session)
    if record.symbol.strip().upper() != symbol:
        raise ValueError("trade symbol does not match the requested symbol")
    return record


def _validated_quote(
    record: NormalizedQuote,
    symbol: str,
    session: CoverageSession,
) -> NormalizedQuote:
    _validate_record_timestamp(record.timestamp, symbol, session)
    if record.symbol.strip().upper() != symbol:
        raise ValueError("quote symbol does not match the requested symbol")
    return record


def _validate_record_timestamp(
    timestamp: datetime,
    symbol: str,
    session: CoverageSession,
) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{symbol} record timestamp must be timezone-aware")
    normalized = timestamp.astimezone(UTC)
    # Both session bounds are inclusive: the provider fetch passes close_time
    # as Alpaca's inclusive end, so the closing auction print stamped exactly
    # at 16:00:00 ET belongs to this session and must not be rejected.
    if not session.open_time <= normalized <= session.close_time:
        raise ValueError(
            f"{symbol} record timestamp is outside session {session.session_date}"
        )


def _coverage_record(
    symbol: str,
    session: CoverageSession,
    data_type: DataType,
    status: CoverageStatus,
    metadata: ParquetMetadata,
) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=data_type,
        symbol=symbol,
        session_date=session.session_date,
        status=status,
        object_path=metadata.path,
        row_count=metadata.rows,
        byte_size=metadata.bytes,
        checksum=metadata.checksum,
    )


__all__ = [
    "AtomicIngestCommitter",
    "EquityIngestResult",
    "EquityIngestTransaction",
    "EquityMarketDataClient",
    "PartitionWriter",
    "StagedParquetObject",
]
