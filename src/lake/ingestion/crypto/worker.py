"""Queue controller and composition for crypto ingestion."""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast

from lake.configuration import QueueSettings
from lake.contracts import AssetClass, DataType, symbol_filename
from lake.coverage import crypto_sessions
from lake.ingestion.common import AtomicIngestCommitter, WriterFactory
from lake.ingestion.crypto.transaction import (
    CryptoClock,
    CryptoIngestTransaction,
    CryptoMarketDataClient,
)
from lake.queue.worker import (
    DeadLetterSink,
    QueueDelivery,
    QueueHandler,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
    WorkerClock,
)
from lake.storage.parquet import ParquetWriter

_DATA_TYPES = (DataType.TRADE, DataType.QUOTE)
_CRYPTO_PAIR = re.compile(r"[A-Z0-9]+/[A-Z0-9]+", re.ASCII)


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


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("crypto clock and timestamps must be timezone-aware")
    return value.astimezone(UTC)


build_crypto_worker = create_crypto_worker
CryptoWorker = CryptoQueueHandler

__all__ = [
    "CryptoQueueHandler",
    "CryptoWorker",
    "build_crypto_worker",
    "create_crypto_worker",
]
