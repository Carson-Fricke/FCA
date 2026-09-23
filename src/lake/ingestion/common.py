"""Shared ports and value objects for atomic asset ingestion."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

import httpx

from lake.catalog.models import CoverageRecord
from lake.contracts import AssetClass, DataType
from lake.storage.parquet import ObjectStore, ParquetMetadata

#: Transient fetch failures (transport level): DNS/connect hiccups, resets,
#: timeouts. Re-running the fetch can succeed. Permanent failures (provider
#: protocol errors, non-retryable HTTP statuses, validation errors) must not
#: be retried - they fail identically forever.
_TRANSIENT_FETCH_ERRORS = (httpx.TransportError, TimeoutError)

#: Attempts and backoff for transient fetch retries within one session.
TRANSIENT_RETRY_ATTEMPTS = 3
TRANSIENT_RETRY_BASE_DELAY_SECONDS = 1.0


def is_transient_fetch_error(error: BaseException) -> bool:
    """Return whether a fetch failure is a transport-level hiccup."""
    return isinstance(error, _TRANSIENT_FETCH_ERRORS)


async def fetch_with_transient_retry[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int | None = None,
    base_delay_seconds: float | None = None,
) -> T:
    """Run one fetch, retrying transient failures with growing backoff.

    This is the transient/permanent discrimination point: only transport
    errors are retried (up to ``attempts``); every other failure propagates
    on the first occurrence.
    """
    budget = TRANSIENT_RETRY_ATTEMPTS if attempts is None else attempts
    base = (
        TRANSIENT_RETRY_BASE_DELAY_SECONDS
        if base_delay_seconds is None
        else base_delay_seconds
    )
    for attempt in range(1, budget + 1):
        try:
            return await call()
        except _TRANSIENT_FETCH_ERRORS:
            if attempt == budget:
                raise
            await asyncio.sleep(base * attempt)
    raise AssertionError("unreachable retry loop")


#: The equity data types an ingest may request, in canonical publication order.
_DATA_TYPES = (DataType.TRADE, DataType.QUOTE)


def normalize_data_types(
    values: Sequence[DataType | str] | DataType | str | None,
) -> tuple[DataType, ...]:
    """Return the requested data types in canonical order.

    ``None`` selects both data types, so a caller that does not care keeps the
    whole-session behaviour. An ensure that asked for exactly one data type
    fetches and publishes only that one: two ensures for the same symbol no
    longer each fetch the other's dataset.
    """
    if values is None:
        return _DATA_TYPES
    if isinstance(values, DataType | str):
        values = (values,)
    selected = {DataType(value) for value in values}
    if not selected or not selected.issubset(_DATA_TYPES):
        raise ValueError("data_types must contain trade or quote")
    return tuple(data_type for data_type in _DATA_TYPES if data_type in selected)


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
    "fetch_with_transient_retry",
    "is_transient_fetch_error",
    "normalize_data_types",
]
