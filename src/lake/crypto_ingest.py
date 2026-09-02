"""Atomic crypto pair-day ingestion and durable queue integration.

The provider client, Parquet writer, object/catalog committer, and clock are
all supplied by the caller.  The transaction stages complete crypto trade and
quote objects privately, then publishes deterministic object paths and T024
coverage rows through one injected commit boundary.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, cast

from lake.alpaca import NormalizedCryptoQuote, NormalizedCryptoTrade
from lake.catalog import CoverageRecord, CoverageStatus
from lake.coverage import CoverageSession, crypto_sessions
from lake.equity_ingest import (
    AtomicIngestCommitter,
    StagedParquetObject,
    WriterFactory,
)
from lake.parquet import ParquetMetadata, ParquetWriter
from lake.partition_paths import AssetClass, DataType, symbol_filename
from lake.queue_worker import (
    DeadLetterSink,
    QueueDelivery,
    QueueHandler,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
    WorkerClock,
)
from lake.settings import QueueSettings


class CryptoMarketDataClient(Protocol):
    """Provider boundary supplied by T057's crypto client or a test fake."""

    async def get_crypto_trades(
        self,
        pair: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoTrade]:
        """Fetch normalized crypto trades for one pair-day range."""

    async def get_crypto_quotes(
        self,
        pair: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoQuote]:
        """Fetch normalized crypto quotes for one pair-day range."""


CryptoClock = Callable[[], datetime]
_DATA_TYPES = (DataType.TRADE, DataType.QUOTE)
_CRYPTO_PAIR = re.compile(r"[A-Z0-9]+/[A-Z0-9]+", re.ASCII)


@dataclass(frozen=True, slots=True)
class CryptoIngestResult:
    """Published metadata for one crypto pair-day transaction."""

    symbol: str
    session_date: date
    coverage: tuple[CoverageRecord, ...]
    objects: tuple[ParquetMetadata, ...]


class CryptoIngestTransaction:
    """Fetch, stage, and atomically publish one crypto pair-day."""

    def __init__(
        self,
        *,
        client: CryptoMarketDataClient,
        committer: AtomicIngestCommitter,
        writer_factory: WriterFactory = ParquetWriter,
        clock: CryptoClock | None = None,
    ) -> None:
        self._client = client
        self._committer = committer
        self._writer_factory = writer_factory
        self._clock = _now_utc if clock is None else clock

    def now(self) -> datetime:
        """Return the injected UTC clock value used for partial-day bounds."""
        return _utc_datetime(self._clock())

    async def run(
        self,
        symbol: str,
        session: CoverageSession,
        *,
        data_types: Sequence[DataType | str] | DataType | str = _DATA_TYPES,
    ) -> CryptoIngestResult:
        """Fetch and publish the requested data types for one UTC day.

        A partial session is queried only through the injected current time.
        All fetches finish before any object is staged, and a fetch or
        normalization error publishes only error coverage through the same
        committer boundary.
        """
        canonical_symbol = _canonical_pair(symbol)
        selected_types = _normalize_data_types(data_types)
        _validate_session(session)
        query_end = min(
            self.now() if session.is_partial else session.close_time,
            session.close_time,
        )
        if query_end < session.open_time:
            raise ValueError("partial crypto session clock is before its opening")
        results = await asyncio.gather(
            *(
                self._fetch(
                    data_type,
                    canonical_symbol,
                    session.open_time,
                    query_end,
                )
                for data_type in selected_types
            ),
            return_exceptions=True,
        )
        fetch_error = _fetch_error(selected_types, results)
        if fetch_error is not None:
            return self._publish_error(
                canonical_symbol, session, selected_types, fetch_error
            )

        records = {
            data_type: cast(
                Sequence[NormalizedCryptoTrade | NormalizedCryptoQuote], result
            )
            for data_type, result in zip(selected_types, results, strict=True)
        }
        try:
            staged, metadata = self._stage(
                canonical_symbol,
                session,
                selected_types,
                records,
                query_end,
            )
        except Exception as error:
            return self._publish_error(
                canonical_symbol,
                session,
                selected_types,
                _error_message(error),
            )

        coverage = tuple(
            self._coverage_record(
                canonical_symbol,
                session,
                data_type,
                metadata[data_type],
                len(records[data_type]),
            )
            for data_type in selected_types
        )
        self._committer.commit(objects=staged, coverage=coverage)
        return CryptoIngestResult(
            symbol=canonical_symbol,
            session_date=session.session_date,
            coverage=coverage,
            objects=tuple(metadata[data_type] for data_type in selected_types),
        )

    async def _fetch(
        self,
        data_type: DataType,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[NormalizedCryptoTrade] | Sequence[NormalizedCryptoQuote]:
        if data_type is DataType.TRADE:
            return await self._client.get_crypto_trades(symbol, start, end)
        return await self._client.get_crypto_quotes(symbol, start, end)

    def _stage(
        self,
        symbol: str,
        session: CoverageSession,
        data_types: Sequence[DataType],
        records: Mapping[
            DataType,
            Sequence[NormalizedCryptoTrade | NormalizedCryptoQuote],
        ],
        query_end: datetime,
    ) -> tuple[tuple[StagedParquetObject, ...], dict[DataType, ParquetMetadata]]:
        store = _StagingObjectStore()
        writer = self._writer_factory(store)
        metadata: dict[DataType, ParquetMetadata] = {}
        for data_type in data_types:
            rows = records[data_type]
            mappings: Sequence[Mapping[str, object]]
            if data_type is DataType.TRADE:
                mappings = _trade_mappings(
                    cast(Sequence[NormalizedCryptoTrade], rows),
                    symbol,
                    session,
                    query_end,
                )
            else:
                mappings = _quote_mappings(
                    cast(Sequence[NormalizedCryptoQuote], rows),
                    symbol,
                    session,
                    query_end,
                )
            metadata[data_type] = writer.write(
                mappings,
                session_date=session.session_date,
                asset_class=AssetClass.CRYPTO,
                data_type=data_type,
                symbol=symbol,
            )
        staged = tuple(
            StagedParquetObject(
                metadata[data_type], store.read(metadata[data_type].path)
            )
            for data_type in data_types
        )
        return staged, metadata

    def _coverage_record(
        self,
        symbol: str,
        session: CoverageSession,
        data_type: DataType,
        metadata: ParquetMetadata,
        row_count: int,
    ) -> CoverageRecord:
        status = (
            CoverageStatus.PARTIAL
            if session.is_partial
            else CoverageStatus.COMPLETE
            if row_count
            else CoverageStatus.EMPTY
        )
        return CoverageRecord(
            asset_class=AssetClass.CRYPTO,
            data_type=data_type,
            symbol=symbol,
            session_date=session.session_date,
            status=status,
            object_path=metadata.path,
            row_count=row_count,
            byte_size=metadata.bytes,
            checksum=metadata.checksum,
        )

    def _publish_error(
        self,
        symbol: str,
        session: CoverageSession,
        data_types: Sequence[DataType],
        message: str,
    ) -> CryptoIngestResult:
        coverage = tuple(
            CoverageRecord(
                asset_class=AssetClass.CRYPTO,
                data_type=data_type,
                symbol=symbol,
                session_date=session.session_date,
                status=CoverageStatus.ERROR,
                error_message=message,
            )
            for data_type in data_types
        )
        self._committer.commit(objects=(), coverage=coverage)
        return CryptoIngestResult(
            symbol=symbol,
            session_date=session.session_date,
            coverage=coverage,
            objects=(),
        )


class CryptoQueueHandler:
    """Decode a planned queue delivery and run its idempotent transaction."""

    def __init__(
        self,
        transaction: CryptoIngestTransaction,
        *,
        clock: CryptoClock | None = None,
    ) -> None:
        self._transaction = transaction
        self._clock = transaction.now if clock is None else clock

    async def __call__(self, delivery: QueueDelivery) -> None:
        """Process one queue message; errors are raised for queue retry."""
        symbol, session_date, data_types = _decode_work(delivery.payload)
        current_time = _utc_datetime(self._clock())
        if current_time < datetime.combine(
            session_date, datetime.min.time(), tzinfo=UTC
        ):
            raise ValueError("crypto queue day is in the future")
        session = crypto_sessions(
            session_date,
            session_date,
            as_of=current_time,
        )[0]
        await self._transaction.run(symbol, session, data_types=data_types)


def create_crypto_worker(
    *,
    queue: QueueTransport,
    client: CryptoMarketDataClient,
    committer: AtomicIngestCommitter,
    policy: QueueWorkerPolicy | None = None,
    settings: QueueSettings | None = None,
    writer_factory: WriterFactory = ParquetWriter,
    dead_letter: DeadLetterSink | None = None,
    clock: WorkerClock | None = None,
    current_clock: CryptoClock | None = None,
    idle_sleep_seconds: float = 1.0,
) -> QueueWorker:
    """Build the shared queue worker around crypto-specific ingestion."""
    if policy is None:
        if settings is None:
            raise ValueError("policy or settings must be supplied")
        policy = QueueWorkerPolicy.from_settings(settings)
    transaction = CryptoIngestTransaction(
        client=client,
        committer=committer,
        writer_factory=writer_factory,
        clock=current_clock,
    )
    handler: QueueHandler = CryptoQueueHandler(transaction, clock=current_clock)
    return QueueWorker(
        queue=queue,
        handler=handler,
        policy=policy,
        dead_letter=dead_letter,
        clock=clock,
        idle_sleep_seconds=idle_sleep_seconds,
    )


build_crypto_worker = create_crypto_worker


CryptoWorker = CryptoQueueHandler


class _StagingObjectStore:
    """Private store that keeps staged bytes invisible until commit."""

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


def _decode_work(
    payload: Mapping[str, object],
) -> tuple[str, date, tuple[DataType, ...]]:
    if payload.get("asset_class") != AssetClass.CRYPTO.value:
        raise ValueError("crypto queue message must use asset_class 'crypto'")
    raw_symbol = payload.get("symbol")
    if not isinstance(raw_symbol, str):
        raise ValueError("crypto queue message symbol must be a string")
    symbol = _canonical_pair(raw_symbol)
    raw_date = payload.get("session_date")
    if not isinstance(raw_date, str):
        raise ValueError("crypto queue message session_date must be a string")
    try:
        session_date = date.fromisoformat(raw_date)
    except ValueError as error:
        raise ValueError(
            "crypto queue message session_date must be ISO-8601"
        ) from error
    raw_types = payload.get("data_types")
    if not isinstance(raw_types, (list, tuple)) or not raw_types:
        raise ValueError("crypto queue message data_types must be a non-empty array")
    if any(not isinstance(value, (DataType, str)) for value in raw_types):
        raise ValueError("crypto queue message data_types must contain strings")
    data_types = _normalize_data_types(cast(Sequence[DataType | str], raw_types))
    _validate_message_bound(payload.get("start_time"), session_date, "start_time")
    end_time = payload.get("end_time")
    if end_time is None:
        raise ValueError("crypto queue message end_time is required")
    parsed_end = _parse_datetime(end_time, "end_time")
    day_start = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    if not day_start <= parsed_end <= day_end:
        raise ValueError("crypto queue message end_time must be within its UTC day")
    return symbol, session_date, data_types


def _validate_message_bound(value: object, session_date: date, field_name: str) -> None:
    if value is None:
        raise ValueError(f"crypto queue message {field_name} is required")
    parsed = _parse_datetime(value, field_name)
    expected = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    if parsed != expected:
        raise ValueError(f"crypto queue message {field_name} must be UTC midnight")


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"crypto queue message {field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"crypto queue message {field_name} must be an ISO timestamp"
        ) from error
    return _utc_datetime(parsed)


def _normalize_data_types(
    values: Sequence[DataType | str] | DataType | str,
) -> tuple[DataType, ...]:
    if isinstance(values, (DataType, str)):
        values = (values,)
    selected = {DataType(value) for value in values}
    if not selected or not selected.issubset(_DATA_TYPES):
        raise ValueError("crypto data_types must contain trades or quotes")
    return tuple(data_type for data_type in _DATA_TYPES if data_type in selected)


def _canonical_pair(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise TypeError("crypto pair must be a string")
    canonical = symbol.strip().upper()
    if _CRYPTO_PAIR.fullmatch(canonical) is None:
        raise ValueError(f"invalid crypto pair: {symbol!r}")
    symbol_filename(canonical)
    return canonical


def _trade_mappings(
    records: Sequence[NormalizedCryptoTrade],
    symbol: str,
    session: CoverageSession,
    query_end: datetime,
) -> list[Mapping[str, object]]:
    ordered = sorted(
        (_validated_trade(record, symbol, session, query_end) for record in records),
        key=lambda record: (
            record.timestamp,
            -1 if record.trade_id is None else record.trade_id,
            record.exchange or "",
            record.price,
            record.size,
            record.taker_side or "",
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
            "taker_side": record.taker_side,
        }
        for record in ordered
    ]


def _quote_mappings(
    records: Sequence[NormalizedCryptoQuote],
    symbol: str,
    session: CoverageSession,
    query_end: datetime,
) -> list[Mapping[str, object]]:
    ordered = sorted(
        (_validated_quote(record, symbol, session, query_end) for record in records),
        key=lambda record: (
            record.timestamp,
            record.bid_exchange or "",
            record.bid_price,
            record.bid_size,
            record.ask_exchange or "",
            record.ask_price,
            record.ask_size,
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
        }
        for record in ordered
    ]


def _validated_trade(
    record: NormalizedCryptoTrade,
    symbol: str,
    session: CoverageSession,
    query_end: datetime,
) -> NormalizedCryptoTrade:
    _validate_record_timestamp(record.timestamp, symbol, session, query_end)
    if _canonical_pair(record.symbol) != symbol:
        raise ValueError("trade symbol does not match the requested pair")
    return record


def _validated_quote(
    record: NormalizedCryptoQuote,
    symbol: str,
    session: CoverageSession,
    query_end: datetime,
) -> NormalizedCryptoQuote:
    _validate_record_timestamp(record.timestamp, symbol, session, query_end)
    if _canonical_pair(record.symbol) != symbol:
        raise ValueError("quote symbol does not match the requested pair")
    return record


def _validate_record_timestamp(
    timestamp: datetime,
    symbol: str,
    session: CoverageSession,
    query_end: datetime,
) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{symbol} record timestamp must be timezone-aware")
    normalized = timestamp.astimezone(UTC)
    if not session.open_time <= normalized < query_end:
        raise ValueError(
            f"{symbol} record timestamp is outside UTC day {session.session_date}"
        )


def _validate_session(session: CoverageSession) -> None:
    expected_open = datetime.combine(
        session.session_date,
        datetime.min.time(),
        tzinfo=UTC,
    )
    if (
        session.open_time != expected_open
        or session.close_time != expected_open + timedelta(days=1)
    ):
        raise ValueError("crypto session must be one UTC calendar day")


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


def _error_message(error: BaseException) -> str:
    detail = str(error).strip()
    return type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("crypto clock and timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "CryptoIngestResult",
    "CryptoIngestTransaction",
    "CryptoMarketDataClient",
    "CryptoQueueHandler",
    "CryptoWorker",
    "build_crypto_worker",
    "create_crypto_worker",
]
