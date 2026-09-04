"""Live market-event recording in exact market-lake schemas.

The :class:`LiveRecorder` persists incoming live events below a
``live/{deployment}`` prefix using the published market-lake Parquet schemas
and Hive partition directories::

    live/{deployment}/date=YYYY-MM-DD/asset_class=<asset>/data_type=<type>/
        <SYMBOL>.part-000000.parquet

Every flush atomically replaces one complete, readable Parquet part, so a
crash at any moment leaves all earlier parts readable for parity and incident
replay, and only the bounded pending batch can be lost.  A partition rotates
when an event arrives for a later session -- UTC calendar days for crypto,
XNYS sessions for equities and options -- and an event for an earlier session
than the active one is rejected so every partition stays ordered.  Nothing in
this module reads wall time, opens a network connection, or uses unseeded
randomness: time comes from event timestamps and an injected
:class:`~harness.core.clock.Clock`, and storage goes through the small
:class:`LiveObjectStore` boundary.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq

from harness.core.clock import Clock
from harness.core.types.common import timestamp as utc_timestamp
from harness.core.types.events import QuoteEvent, TradeEvent
from lake.contracts import (
    AssetClass,
    DataType,
    ExchangeSession,
    partition_path,
    schema_for,
    xnys_sessions,
)

type MarketEvent = QuoteEvent | TradeEvent

_DEPLOYMENT_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_LIVE_ROOT: Final = "live"
_PART_DIGITS: Final = 6
_UINT32_MAX: Final = 2**32 - 1
_UINT64_MAX: Final = 2**64 - 1
_INT64_MAX: Final = 2**63 - 1
_SESSION_WINDOW: Final = timedelta(days=10)
_SESSION_CACHE_LIMIT: Final = 8


@runtime_checkable
class LiveObjectStore(Protocol):
    """Whole-object storage boundary used by :class:`LiveRecorder`.

    ``replace`` has whole-object semantics: a successful call makes the
    supplied payload the complete contents at ``relative_path``, so readers
    never observe a partially written part file.
    """

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Atomically replace the object at a deployment-relative path."""


@runtime_checkable
class SessionResolver(Protocol):
    """Maps one event timestamp to its lake session date."""

    def session_date(self, moment: datetime) -> date:
        """Return the lake session date that owns ``moment``."""


class UtcDaySessionResolver:
    """Crypto session semantics: one session per UTC calendar day."""

    __slots__ = ()

    def session_date(self, moment: datetime) -> date:
        """Return the UTC calendar date of ``moment``."""
        return utc_timestamp(moment).date()


class XnysSessionResolver:
    """XNYS session semantics for equities and options.

    Sessions come from the lake session contract.  Regular-session events
    belong to their own session, and events in the gap between two sessions
    belong to the nearer session, decided by the midpoint between the
    previous close and the next open.  This keeps premarket and postmarket
    events on their own session date and overnight and weekend events on the
    next session, without consulting any wall-clock time zone.
    """

    __slots__ = ("_cache",)

    def __init__(self) -> None:
        self._cache: dict[date, tuple[ExchangeSession, ...]] = {}

    def session_date(self, moment: datetime) -> date:
        """Return the XNYS session date that owns ``moment``."""
        stamp = utc_timestamp(moment)
        sessions = self._sessions_for(stamp.date())
        previous = sessions[0]
        if stamp < previous.open_time:
            return previous.session_date
        for candidate in sessions[1:]:
            if stamp < candidate.open_time:
                if stamp < previous.close_time:
                    return previous.session_date
                midpoint = (
                    previous.close_time
                    + (candidate.open_time - previous.close_time) / 2
                )
                return (
                    previous.session_date
                    if stamp < midpoint
                    else (candidate.session_date)
                )
            previous = candidate
        return previous.session_date

    def _sessions_for(self, day: date) -> tuple[ExchangeSession, ...]:
        cached = self._cache.get(day)
        if cached is None:
            cached = xnys_sessions(day - _SESSION_WINDOW, day + _SESSION_WINDOW)
            if len(self._cache) >= _SESSION_CACHE_LIMIT:
                self._cache.pop(next(iter(self._cache)))
            self._cache[day] = cached
        return cached


class LocalLiveObjectStore:
    """Local object store with atomic whole-file replacement.

    It mirrors the market-lake ``ObjectStore`` replacement semantics so a
    live recording can be pointed at a workstation directory without any
    network or cloud dependency.
    """

    __slots__ = ("_root",)

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Atomically write ``payload`` as the complete object at the path."""
        target = self._target(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def read(self, relative_path: str) -> bytes:
        """Read one object, primarily for verification and incident replay."""
        return self._target(relative_path).read_bytes()

    def _target(self, relative_path: str) -> Path:
        if (
            not relative_path
            or "\\" in relative_path
            or relative_path.startswith("/")
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise ValueError("object path must be a non-empty relative POSIX path")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute():
            raise ValueError("object path must be relative")
        target = (self._root / Path(*relative.parts)).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError("object path escapes the store root")
        return target


def _deployment_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("deployment must be a string")
    canonical = value.strip()
    if canonical in {".", ".."} or _DEPLOYMENT_PATTERN.fullmatch(canonical) is None:
        raise ValueError(
            "deployment must be one safe path segment of letters, digits, "
            "dots, dashes, or underscores"
        )
    return canonical


def live_part_path(
    *,
    deployment: str,
    session_date: date,
    asset_class: AssetClass | str,
    data_type: DataType | str,
    symbol: str,
    part: int,
) -> str:
    """Build one live part path below the ``live/{deployment}`` prefix.

    The Hive partition directories reuse the market-lake partition contract
    exactly; the part suffix on the canonical symbol filename keeps every
    flushed object complete and individually readable.
    """
    if isinstance(session_date, datetime):
        raise TypeError("session_date must be a date, not a datetime")
    if isinstance(part, bool) or not isinstance(part, int) or part < 0:
        raise ValueError("part must be a non-negative integer")
    directory = partition_path(
        session_date=session_date,
        asset_class=AssetClass(asset_class),
        data_type=DataType(data_type),
        symbol=symbol,
    )
    head, _, filename = directory.rpartition("/")
    stem = filename.removesuffix(".parquet")
    return (
        f"{_LIVE_ROOT}/{_deployment_text(deployment)}/{head}/"
        f"{stem}.part-{part:0{_PART_DIGITS}d}.parquet"
    )


def _integer_id(
    value: str | None, field_name: str, *, required: bool, upper: int
) -> int | None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required by the lake schema")
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an integer identifier") from error
    if parsed < 0 or parsed > upper:
        raise ValueError(f"{field_name} exceeds the lake identifier range")
    return parsed


def _integer_size(value: Decimal, field_name: str) -> int:
    if value != value.to_integral_value():
        raise ValueError(f"{field_name} must be an integral lake size")
    size = int(value)
    if not 0 < size <= _UINT32_MAX:
        raise ValueError(f"{field_name} exceeds the lake uint32 size range")
    return size


def _equity_trade_row(event: TradeEvent) -> dict[str, object]:
    return {
        "timestamp": event.timestamp,
        "symbol": str(event.symbol),
        "trade_id": _integer_id(
            event.trade_id, "trade_id", required=True, upper=_UINT64_MAX
        ),
        "exchange": event.venue or "",
        "price": event.price,
        "size": _integer_size(event.quantity, "size"),
        "conditions": [],
        "tape": "",
        "update": None,
    }


def _option_trade_row(event: TradeEvent) -> dict[str, object]:
    return {
        "timestamp": event.timestamp,
        "symbol": str(event.symbol),
        "exchange": event.venue or "",
        "price": event.price,
        "size": _integer_size(event.quantity, "size"),
        "condition": "",
    }


def _crypto_trade_row(event: TradeEvent) -> dict[str, object]:
    return {
        "timestamp": event.timestamp,
        "symbol": str(event.symbol),
        "trade_id": _integer_id(
            event.trade_id, "trade_id", required=False, upper=_INT64_MAX
        ),
        "exchange": event.venue,
        "price": event.price,
        "size": event.quantity,
        "taker_side": None,
    }


def _equity_quote_row(event: QuoteEvent) -> dict[str, object]:
    return {
        "timestamp": event.timestamp,
        "symbol": str(event.symbol),
        "bid_exchange": event.venue or "",
        "bid_price": event.bid_price,
        "bid_size": _integer_size(event.bid_quantity, "bid_size"),
        "ask_exchange": event.venue or "",
        "ask_price": event.ask_price,
        "ask_size": _integer_size(event.ask_quantity, "ask_size"),
        "conditions": [],
        "tape": "",
    }


def _option_quote_row(event: QuoteEvent) -> dict[str, object]:
    return {
        "timestamp": event.timestamp,
        "symbol": str(event.symbol),
        "bid_exchange": event.venue or "",
        "bid_price": event.bid_price,
        "bid_size": _integer_size(event.bid_quantity, "bid_size"),
        "ask_exchange": event.venue or "",
        "ask_price": event.ask_price,
        "ask_size": _integer_size(event.ask_quantity, "ask_size"),
        "condition": "",
    }


def _crypto_quote_row(event: QuoteEvent) -> dict[str, object]:
    return {
        "timestamp": event.timestamp,
        "symbol": str(event.symbol),
        "bid_exchange": event.venue,
        "bid_price": event.bid_price,
        "bid_size": event.bid_quantity,
        "ask_exchange": event.venue,
        "ask_price": event.ask_price,
        "ask_size": event.ask_quantity,
    }


def _quote_row_for(event: QuoteEvent, asset_class: AssetClass) -> dict[str, object]:
    if asset_class is AssetClass.EQUITY:
        return _equity_quote_row(event)
    if asset_class is AssetClass.OPTION:
        return _option_quote_row(event)
    return _crypto_quote_row(event)


def _trade_row_for(event: TradeEvent, asset_class: AssetClass) -> dict[str, object]:
    if asset_class is AssetClass.EQUITY:
        return _equity_trade_row(event)
    if asset_class is AssetClass.OPTION:
        return _option_trade_row(event)
    return _crypto_trade_row(event)


def _default_resolver(asset_class: AssetClass) -> SessionResolver:
    if asset_class is AssetClass.CRYPTO:
        return UtcDaySessionResolver()
    return XnysSessionResolver()


class _PartitionWriter:
    """Buffer one partition's rows and replace one complete part per flush."""

    __slots__ = (
        "_asset_class",
        "_batch_size",
        "_compression",
        "_data_type",
        "_deployment",
        "_oldest",
        "_part",
        "_pending",
        "_rows",
        "_schema",
        "_session_date",
        "_store",
        "_symbol",
        "_version",
    )

    def __init__(
        self,
        *,
        deployment: str,
        store: LiveObjectStore,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
        session_date: date,
        schema: pa.Schema,
        batch_size: int,
        compression: str,
        version: str,
    ) -> None:
        self._deployment = deployment
        self._store = store
        self._asset_class = asset_class
        self._data_type = data_type
        self._symbol = symbol
        self._session_date = session_date
        self._schema = schema
        self._batch_size = batch_size
        self._compression = compression
        self._version = version
        self._pending: list[dict[str, object]] = []
        self._part = 0
        self._rows = 0
        self._oldest: datetime | None = None

    @property
    def rows_written(self) -> int:
        """Return rows already replaced into part files."""
        return self._rows

    @property
    def pending_rows(self) -> int:
        """Return rows buffered in memory and not yet written."""
        return len(self._pending)

    def append(self, row: Mapping[str, object], event_time: datetime) -> None:
        """Buffer one row and flush when the batch is full."""
        if self._oldest is None:
            self._oldest = event_time
        self._pending.append(dict(row))
        if len(self._pending) >= self._batch_size:
            self.flush()

    def is_stale(self, now: datetime, interval: timedelta) -> bool:
        """Return whether pending rows have aged past ``interval`` by ``now``."""
        return self._oldest is not None and now - self._oldest >= interval

    def flush(self) -> None:
        """Replace one complete, readable part file with the pending rows."""
        if not self._pending:
            return
        table = pa.Table.from_pylist(self._pending, schema=self._schema)
        buffer = io.BytesIO()
        pq.write_table(
            table,
            buffer,
            compression=self._compression,
            version=self._version,
            use_compliant_nested_type=False,
        )
        payload = buffer.getvalue()
        path = live_part_path(
            deployment=self._deployment,
            session_date=self._session_date,
            asset_class=self._asset_class,
            data_type=self._data_type,
            symbol=self._symbol,
            part=self._part,
        )
        self._store.replace(path, payload)
        self._rows += table.num_rows
        self._part += 1
        self._pending.clear()
        self._oldest = None

    def close(self) -> None:
        """Flush the pending batch so shutdown leaves every file readable."""
        self.flush()


@dataclass(frozen=True, slots=True)
class LiveRecorderConfig:
    """Immutable configuration for one live recording deployment."""

    deployment: str
    asset_class: AssetClass | str
    store: LiveObjectStore | str | os.PathLike[str]
    clock: Clock
    batch_size: int = 4096
    compression: str = "zstd"
    parquet_version: str = "2.6"
    flush_interval: timedelta | None = None
    session_resolver: SessionResolver | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment", _deployment_text(self.deployment))
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        store = self.store
        if isinstance(store, (str, os.PathLike)):
            object.__setattr__(self, "store", LocalLiveObjectStore(store))
        elif not isinstance(store, LiveObjectStore):
            raise TypeError("store must be a LiveObjectStore or a local root path")
        if not isinstance(self.clock, Clock):
            raise TypeError("clock must implement Clock")
        batch_size = self.batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError("batch_size must be a positive integer")
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        for name in ("compression", "parquet_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        interval = self.flush_interval
        if interval is not None:
            if not isinstance(interval, timedelta):
                raise TypeError("flush_interval must be a timedelta or None")
            if interval < timedelta(0):
                raise ValueError("flush_interval must not be negative")
        resolver = self.session_resolver
        if resolver is not None and not isinstance(resolver, SessionResolver):
            raise TypeError("session_resolver must implement SessionResolver")


class LiveRecorder:
    """Persist live market events as lake-schema Parquet part files.

    Events stream in one at a time through :meth:`record` and land in the
    exact lake schema for the configured asset class.  Each
    ``(data_type, symbol)`` partition keeps at most ``batch_size`` rows in
    memory before atomically replacing one complete Parquet part, so a crash
    loses only the bounded pending batch.  When an event arrives for a later
    session the previous session's writer is finalized first, and an event
    for an earlier session than the active one is rejected.
    """

    __slots__ = (
        "_asset_class",
        "_closed",
        "_config",
        "_finalized_rows",
        "_resolver",
        "_sessions",
        "_store",
        "_writers",
    )

    def __init__(self, config: LiveRecorderConfig) -> None:
        if not isinstance(config, LiveRecorderConfig):
            raise TypeError("config must be a LiveRecorderConfig")
        self._config = config
        self._asset_class = AssetClass(config.asset_class)
        raw_store = config.store
        self._store: LiveObjectStore = (
            LocalLiveObjectStore(raw_store)
            if isinstance(raw_store, (str, os.PathLike))
            else raw_store
        )
        self._resolver: SessionResolver = (
            config.session_resolver
            if config.session_resolver is not None
            else _default_resolver(self._asset_class)
        )
        self._writers: dict[tuple[DataType, str], _PartitionWriter] = {}
        self._sessions: dict[tuple[DataType, str], date] = {}
        self._finalized_rows: dict[tuple[DataType, str], int] = {}
        self._closed = False

    @property
    def config(self) -> LiveRecorderConfig:
        """Return the recorder configuration."""
        return self._config

    @property
    def closed(self) -> bool:
        """Whether the recording has been finalized."""
        return self._closed

    @property
    def pending_rows(self) -> int:
        """Return the total rows buffered across active partitions."""
        return sum(writer.pending_rows for writer in self._writers.values())

    @property
    def row_counts(self) -> Mapping[str, int]:
        """Return rows written to part files per ``data_type/symbol`` key."""
        counts: dict[str, int] = {
            f"{key[0].value}/{key[1]}": rows
            for key, rows in self._finalized_rows.items()
        }
        for partition, writer in self._writers.items():
            name = f"{partition[0].value}/{partition[1]}"
            counts[name] = self._finalized_rows.get(partition, 0) + writer.rows_written
        return MappingProxyType(counts)

    def __enter__(self) -> LiveRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def record(self, event: MarketEvent) -> None:
        """Persist one incoming live market event."""
        if self._closed:
            raise RuntimeError("the live recorder is closed")
        if isinstance(event, QuoteEvent):
            data_type = DataType.QUOTE
        elif isinstance(event, TradeEvent):
            data_type = DataType.TRADE
        else:
            raise TypeError("event must be a QuoteEvent or TradeEvent")
        symbol = str(event.symbol)
        session = self._resolver.session_date(event.timestamp)
        partition = (data_type, symbol)
        active = self._sessions.get(partition)
        if active is not None and session < active:
            raise ValueError(
                f"event session {session.isoformat()} precedes the active "
                f"session {active.isoformat()} for "
                f"{data_type.value}/{symbol}"
            )
        if active is None or session > active:
            self._rotate(partition, session)
        self._flush_stale()
        row = (
            _quote_row_for(event, self._asset_class)
            if isinstance(event, QuoteEvent)
            else _trade_row_for(event, self._asset_class)
        )
        self._writers[partition].append(row, event.timestamp)

    def flush(self) -> None:
        """Replace one complete part per partition carrying pending rows."""
        if self._closed:
            raise RuntimeError("the live recorder is closed")
        for writer in self._writers.values():
            writer.flush()

    def close(self) -> None:
        """Flush every pending batch so shutdown leaves every file readable."""
        if self._closed:
            return
        for writer in self._writers.values():
            writer.close()
        self._closed = True

    def _rotate(self, partition: tuple[DataType, str], session: date) -> None:
        writer = self._writers.pop(partition, None)
        if writer is not None:
            writer.close()
            self._finalized_rows[partition] = (
                self._finalized_rows.get(partition, 0) + writer.rows_written
            )
        self._sessions[partition] = session
        self._writers[partition] = _PartitionWriter(
            deployment=self._config.deployment,
            store=self._store,
            asset_class=self._asset_class,
            data_type=partition[0],
            symbol=partition[1],
            session_date=session,
            schema=schema_for(self._asset_class, partition[0]),
            batch_size=self._config.batch_size,
            compression=self._config.compression,
            version=self._config.parquet_version,
        )

    def _flush_stale(self) -> None:
        interval = self._config.flush_interval
        if interval is None:
            return
        now = utc_timestamp(self._config.clock.now())
        for writer in self._writers.values():
            if writer.is_stale(now, interval):
                writer.flush()


__all__ = [
    "LocalLiveObjectStore",
    "LiveObjectStore",
    "LiveRecorder",
    "LiveRecorderConfig",
    "MarketEvent",
    "SessionResolver",
    "UtcDaySessionResolver",
    "XnysSessionResolver",
    "live_part_path",
]
