"""Atomic, dependency-injected ingestion for one equity symbol session.

The existing Parquet and catalog protocols each represent one resource. This
module adds the transaction boundary needed to publish both data types and
their coverage rows as one commit supplied by the caller.
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
    ) -> EquityIngestResult:
        """Fetch and publish trades and quotes for one planned session.

        Both provider requests complete before staging begins. Fetch or
        normalization failures publish only error metadata through the same
        atomic boundary; a failed commit is propagated without a compensating
        write that could hide a partial backend failure.
        """
        canonical_symbol = _canonical_symbol(symbol)
        if self._storage_guard is not None:
            self._storage_guard.enforce_ingest()
        trade_result, quote_result = await asyncio.gather(
            self._client.get_trades(
                canonical_symbol, session.open_time, session.close_time
            ),
            self._client.get_quotes(
                canonical_symbol, session.open_time, session.close_time
            ),
            return_exceptions=True,
        )

        fetch_error = _fetch_error(trade_result, quote_result)
        if fetch_error is not None:
            return self._publish_error(canonical_symbol, session, fetch_error)

        trades = cast(Sequence[NormalizedTrade], trade_result)
        quotes = cast(Sequence[NormalizedQuote], quote_result)
        try:
            staged, metadata = self._stage(
                canonical_symbol,
                session,
                trades,
                quotes,
            )
        except Exception as error:
            return self._publish_error(
                canonical_symbol,
                session,
                _error_message(error),
            )

        if self._storage_guard is not None:
            self._storage_guard.enforce_ingest(
                additional_bytes=sum(item.metadata.bytes for item in staged)
            )

        coverage = self._coverage(
            canonical_symbol,
            session,
            metadata,
            trade_rows=len(trades),
            quote_rows=len(quotes),
            trade_truncated=_truncated(trade_result),
            quote_truncated=_truncated(quote_result),
        )
        self._committer.commit(objects=staged, coverage=coverage)
        return EquityIngestResult(
            symbol=canonical_symbol,
            session_date=session.session_date,
            coverage=coverage,
            objects=metadata,
        )

    def _stage(
        self,
        symbol: str,
        session: CoverageSession,
        trades: Sequence[NormalizedTrade],
        quotes: Sequence[NormalizedQuote],
    ) -> tuple[
        tuple[StagedParquetObject, ...],
        tuple[ParquetMetadata, ParquetMetadata],
    ]:
        store = _StagingObjectStore()
        writer = self._writer_factory(store)
        trade_metadata = writer.write(
            _trade_mappings(trades, symbol, session),
            session_date=session.session_date,
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol=symbol,
        )
        quote_metadata = writer.write(
            _quote_mappings(quotes, symbol, session),
            session_date=session.session_date,
            asset_class=AssetClass.EQUITY,
            data_type=DataType.QUOTE,
            symbol=symbol,
        )
        metadata: tuple[ParquetMetadata, ParquetMetadata] = (
            trade_metadata,
            quote_metadata,
        )
        staged = tuple(
            StagedParquetObject(item, store.read(item.path)) for item in metadata
        )
        return staged, metadata

    def _coverage(
        self,
        symbol: str,
        session: CoverageSession,
        metadata: tuple[ParquetMetadata, ParquetMetadata],
        *,
        trade_rows: int,
        quote_rows: int,
        trade_truncated: bool,
        quote_truncated: bool,
    ) -> tuple[CoverageRecord, CoverageRecord]:
        trade_status = _coverage_status(session.is_partial, trade_truncated, trade_rows)
        quote_status = _coverage_status(session.is_partial, quote_truncated, quote_rows)
        return (
            _coverage_record(
                symbol, session, DataType.TRADE, trade_status, metadata[0]
            ),
            _coverage_record(
                symbol, session, DataType.QUOTE, quote_status, metadata[1]
            ),
        )

    def _publish_error(
        self,
        symbol: str,
        session: CoverageSession,
        message: str,
    ) -> EquityIngestResult:
        coverage = (
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol=symbol,
                session_date=session.session_date,
                status=CoverageStatus.ERROR,
                error_message=message,
            ),
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.QUOTE,
                symbol=symbol,
                session_date=session.session_date,
                status=CoverageStatus.ERROR,
                error_message=message,
            ),
        )
        self._committer.commit(objects=(), coverage=coverage)
        return EquityIngestResult(
            symbol=symbol,
            session_date=session.session_date,
            coverage=coverage,
            objects=(),
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


def _fetch_error(trades: object, quotes: object) -> str | None:
    errors: list[str] = []
    for name, result in (("trade", trades), ("quote", quotes)):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            errors.append(f"{name}: {_error_message(result)}")
    return "; ".join(errors) if errors else None


def _error_message(error: BaseException) -> str:
    detail = str(error).strip()
    return type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"


def _truncated(result: object) -> bool:
    """Return whether a fetch result was truncated by the client's budget."""
    return bool(getattr(result, "truncated", False))


def _coverage_status(
    session_partial: bool, truncated: bool, rows: int
) -> CoverageStatus:
    """Return the coverage status for one data type.

    A truncated fetch is partial even for a closed session so a later ensure
    repairs the missing tail instead of sealing an incomplete day.
    """
    if session_partial or truncated:
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
    if not session.open_time <= normalized < session.close_time:
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
