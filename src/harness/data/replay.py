"""Deterministic, bounded replay of market-lake quote and trade partitions.

The feed is intentionally an adapter rather than a storage client.  Callers
provide either already-normalized event streams or a reader for the file
objects returned by the market-lake contract.  A replay owns at most one
lookahead event per active source and activates file readers only for the
current session.
"""

from __future__ import annotations

import hashlib
import heapq
import io
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from harness.core.events import EVENT_CLASS_PRIORITY, EventBus
from harness.core.symbols import canonical_symbol
from harness.core.types import Event, EventType, ParquetCodec, QuoteEvent, TradeEvent
from harness.core.types.common import exact_decimal
from lake.contracts import AssetClass as LakeAssetClass
from lake.contracts import DataType as LakeDataType
from lake.contracts import ExchangeSession, partition_path
from lakeclient import DataType, FileQueryFile

MarketEvent = QuoteEvent | TradeEvent
StreamFactory = Callable[[], Iterable[MarketEvent]]


class ParquetReader(Protocol):
    """Injected reader for one complete lake file.

    Implementations should return an iterable of ``QuoteEvent`` or
    ``TradeEvent`` values.  Mapping rows and Arrow batch readers are accepted
    too, which keeps storage and network concerns outside this module.
    """

    def __call__(self, file: FileQueryFile) -> object:
        """Return a lazy event or row iterable for ``file``."""


@dataclass(frozen=True, slots=True)
class ReplayStream:
    """One explicitly ordered, per-symbol event stream.

    ``events`` may be a reusable iterable or a zero-argument factory.  A
    factory is useful when the same feed object is intentionally replayed more
    than once without caching the source rows.
    """

    symbol: str
    data_type: DataType | str | None
    events: Iterable[MarketEvent] | StreamFactory

    def __post_init__(self) -> None:
        symbol = _canonical_symbol(self.symbol)
        data_type = None if self.data_type is None else DataType(self.data_type)
        if not callable(self.events) and not isinstance(self.events, Iterable):
            raise TypeError("events must be iterable or a zero-argument factory")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "data_type", data_type)

    def open(self) -> Iterable[MarketEvent]:
        """Open the stream without materializing it."""
        if callable(self.events):
            return cast(StreamFactory, self.events)()
        return self.events


@dataclass(frozen=True, slots=True)
class ReplayFingerprints:
    """Stable hashes for the selected files, emitted rows, and complete data."""

    file: str
    row: str
    data: str

    @property
    def file_fingerprint(self) -> str:
        """Return the selected-file fingerprint."""
        return self.file

    @property
    def row_fingerprint(self) -> str:
        """Return the ordered emitted-row fingerprint."""
        return self.row

    @property
    def data_fingerprint(self) -> str:
        """Return the complete replay fingerprint."""
        return self.data


@dataclass(frozen=True, slots=True)
class _Source:
    symbol: str
    data_type: DataType | None
    session_date: date | None
    order: int
    name: str
    factory: StreamFactory


@dataclass(order=True, slots=True)
class _HeapItem:
    timestamp: datetime
    sequence: int
    class_priority: int
    source_order: int
    row_order: int
    event: MarketEvent = field(compare=False)


class _Cursor:
    """A one-lookahead cursor over one source."""

    __slots__ = ("_factory", "_head", "_iterator", "_last_key", "_row_order")

    def __init__(self, factory: StreamFactory) -> None:
        self._factory = factory
        self._head: MarketEvent | None = None
        self._iterator: Iterator[MarketEvent] | None = None
        self._last_key: tuple[datetime, int, int] | None = None
        self._row_order = 0

    def peek(self) -> MarketEvent | None:
        """Return the current event, opening the source lazily."""
        if self._head is None:
            self._advance()
        return self._head

    def pop(self) -> MarketEvent:
        """Remove and return the current event."""
        event = self.peek()
        if event is None:
            raise IndexError("cannot pop an exhausted replay source")
        self._head = None
        return event

    @property
    def row_order(self) -> int:
        """Return the source-local ordinal of the current event."""
        return self._row_order - 1

    def _advance(self) -> None:
        iterator = self._iterator
        if iterator is None:
            iterator = iter(self._factory())
            self._iterator = iterator
        try:
            candidate = next(iterator)
        except StopIteration:
            self._head = None
            return
        event = _require_event(candidate)
        key = (
            event.timestamp,
            event.sequence,
            EVENT_CLASS_PRIORITY[event.event_type],
        )
        if self._last_key is not None and key < self._last_key:
            raise ValueError(
                "each replay source must be ordered by timestamp and sequence"
            )
        self._last_key = key
        self._head = event
        self._row_order += 1


class _FingerprintState:
    __slots__ = ("data", "row")

    def __init__(
        self,
        file_fingerprint: str,
        sessions: Sequence[ExchangeSession] = (),
    ) -> None:
        self.row = hashlib.sha256()
        self.data = hashlib.sha256()
        self.data.update(b"fca-lake-replay-data-v1\0")
        _update_frame(self.data, file_fingerprint.encode("ascii"))
        for session in sessions:
            _update_frame(
                self.data,
                _canonical_fields(
                    (
                        session.session_date.isoformat(),
                        session.open_time.isoformat(timespec="microseconds"),
                        session.close_time.isoformat(timespec="microseconds"),
                    )
                ),
            )

    def add(self, event: MarketEvent) -> None:
        payload = _event_fingerprint_bytes(event)
        _update_frame(self.row, payload)
        _update_frame(self.data, payload)

    def fingerprints(self, file_fingerprint: str) -> ReplayFingerprints:
        return ReplayFingerprints(
            file=file_fingerprint,
            row=self.row.hexdigest(),
            data=self.data.hexdigest(),
        )


class LakeReplayFeed:
    """Stream deterministic market events one explicit session at a time.

    ``sessions`` accepts ``ExchangeSession`` values or explicit UTC dates.  A
    bare date denotes the half-open UTC day ``[00:00, 24:00)``; callers replaying
    equities should normally pass the exact sessions returned by the lake
    session contract.

    Stream mapping values can be a single iterable under a symbol key,
    ``{"quote": iterable, "trade": iterable}`` under a symbol key, or a
    ``ReplayStream``.  ``files`` are normally the ``MarketLakeLoad.files`` from
    the lake adapter.  ``reader`` is called with one file only when that file's
    session is active.
    """

    __slots__ = (
        "_file_fingerprint",
        "_files",
        "_last_fingerprints",
        "_sessions",
        "_stream_sources",
    )

    def __init__(
        self,
        sessions: Iterable[ExchangeSession | date],
        streams: Mapping[object, object] | Sequence[ReplayStream] | None = None,
        *,
        quote_streams: Mapping[str, object] | None = None,
        trade_streams: Mapping[str, object] | None = None,
        files: Iterable[FileQueryFile] = (),
        reader: ParquetReader | None = None,
    ) -> None:
        self._sessions = _normalize_sessions(sessions)
        if streams is not None and (
            quote_streams is not None or trade_streams is not None
        ):
            raise TypeError(
                "streams cannot be combined with quote_streams or trade_streams"
            )
        if streams is None and (quote_streams is not None or trade_streams is not None):
            streams = _separate_streams(quote_streams, trade_streams)
        self._stream_sources = _normalize_streams(streams)
        self._files = _normalize_files(files)
        _validate_file_sessions(self._files, self._sessions)
        if self._files and reader is None:
            raise TypeError("reader is required when files are provided")
        if reader is not None and not callable(reader):
            raise TypeError("reader must be callable")
        self._file_fingerprint = _file_fingerprint(self._stream_sources, self._files)
        self._last_fingerprints = ReplayFingerprints(
            self._file_fingerprint,
            hashlib.sha256().hexdigest(),
            _empty_data_fingerprint(self._file_fingerprint, self._sessions),
        )

        if reader is not None:
            file_reader = reader
            file_sources = tuple(
                _Source(
                    symbol=_canonical_symbol(file.symbol),
                    data_type=file.data_type,
                    session_date=file.session_date,
                    order=len(self._stream_sources) + index,
                    name=file.object_path,
                    factory=lambda file=file, file_reader=file_reader: _reader_events(
                        file_reader(file), file
                    ),
                )
                for index, file in enumerate(self._files)
            )
            self._stream_sources = (*self._stream_sources, *file_sources)

    @property
    def sessions(self) -> tuple[ExchangeSession, ...]:
        """Return the explicit replay sessions in chronological order."""
        return self._sessions

    @property
    def file_fingerprint(self) -> str:
        """Return the stable selected-file/source fingerprint."""
        return self._file_fingerprint

    @property
    def row_fingerprint(self) -> str:
        """Return the latest ordered-row fingerprint."""
        return self._last_fingerprints.row

    @property
    def data_fingerprint(self) -> str:
        """Return the latest complete-data fingerprint."""
        return self._last_fingerprints.data

    @property
    def fingerprints(self) -> ReplayFingerprints:
        """Return all latest replay fingerprints as one immutable value."""
        return self._last_fingerprints

    # Short aliases are useful at manifest boundaries while the descriptive
    # names above remain the primary API.
    @property
    def file_hash(self) -> str:
        return self.file_fingerprint

    @property
    def row_hash(self) -> str:
        return self.row_fingerprint

    @property
    def data_hash(self) -> str:
        return self.data_fingerprint

    def __iter__(self) -> Iterator[MarketEvent]:
        """Yield merged events without collecting a session or full run."""
        state = _FingerprintState(self._file_fingerprint, self._sessions)
        cursors = {
            source.order: _Cursor(source.factory) for source in self._stream_sources
        }
        for session in self._sessions:
            for event in self._merge_session(session, cursors):
                state.add(event)
                yield event
        self._last_fingerprints = state.fingerprints(self._file_fingerprint)

    def session_events(self, session: ExchangeSession | date) -> Iterator[MarketEvent]:
        """Yield one explicit session without opening other file partitions."""
        selected = _normalize_sessions((session,))[0]
        if selected not in self._sessions:
            raise ValueError("session is not part of this replay feed")
        cursors = {
            source.order: _Cursor(source.factory) for source in self._stream_sources
        }
        yield from self._merge_session(selected, cursors)

    def publish(self, bus: EventBus) -> int:
        """Publish the lazy feed into an injected deterministic event bus.

        The feed itself remains bounded; callers that need bounded queue memory
        should consume ``iter(feed)`` and drain the bus between publications.
        """
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        count = 0
        for event in self:
            bus.publish(event)
            count += 1
        return count

    def _merge_session(
        self,
        session: ExchangeSession,
        cursors: Mapping[int, _Cursor],
    ) -> Iterator[MarketEvent]:
        heap: list[_HeapItem] = []
        for source in self._stream_sources:
            if (
                source.session_date is not None
                and source.session_date != session.session_date
            ):
                continue
            cursor = cursors[source.order]
            event = cursor.peek()
            while event is not None and event.timestamp < session.open_time:
                cursor.pop()
                event = cursor.peek()
            if event is not None and event.timestamp < session.close_time:
                _validate_source_event(event, source)
                heapq.heappush(
                    heap,
                    _HeapItem(
                        event.timestamp,
                        event.sequence,
                        EVENT_CLASS_PRIORITY[event.event_type],
                        source.order,
                        cursor.row_order,
                        event,
                    ),
                )

        while heap:
            item = heapq.heappop(heap)
            source = self._stream_sources[item.source_order]
            cursor = cursors[source.order]
            event = cursor.pop()
            if event is not item.event:
                raise RuntimeError("replay cursor changed its lookahead unexpectedly")
            yield event
            next_event = cursor.peek()
            if next_event is not None and next_event.timestamp < session.close_time:
                _validate_source_event(next_event, source)
                heapq.heappush(
                    heap,
                    _HeapItem(
                        next_event.timestamp,
                        next_event.sequence,
                        EVENT_CLASS_PRIORITY[next_event.event_type],
                        source.order,
                        cursor.row_order,
                        next_event,
                    ),
                )


def _normalize_sessions(
    sessions: Iterable[ExchangeSession | date],
) -> tuple[ExchangeSession, ...]:
    values: list[ExchangeSession] = []
    for value in sessions:
        if isinstance(value, ExchangeSession):
            session = value
        elif isinstance(value, datetime) or not isinstance(value, date):
            raise TypeError("sessions must contain ExchangeSession or date values")
        else:
            start = datetime.combine(value, datetime.min.time(), UTC)
            session = ExchangeSession(value, start, start + timedelta(days=1))
        if session.open_time.tzinfo is None or session.close_time.tzinfo is None:
            raise ValueError("session bounds must be timezone-aware")
        if session.close_time <= session.open_time:
            raise ValueError("session close_time must be after open_time")
        values.append(
            ExchangeSession(
                session.session_date,
                session.open_time.astimezone(UTC),
                session.close_time.astimezone(UTC),
            )
        )
    if not values:
        raise ValueError("at least one replay session is required")
    ordered = tuple(sorted(values, key=lambda value: value.session_date))
    if len({value.session_date for value in ordered}) != len(ordered):
        raise ValueError("replay sessions must not contain duplicate dates")
    return ordered


def _normalize_streams(
    streams: Mapping[object, object] | Sequence[ReplayStream] | None,
) -> tuple[_Source, ...]:
    if streams is None:
        return ()
    values: list[ReplayStream] = []
    if isinstance(streams, Mapping):
        for key, value in streams.items():
            symbol, data_type = _stream_key(key)
            if isinstance(value, ReplayStream):
                values.append(value)
            elif isinstance(value, Mapping):
                for nested_type, nested_events in value.items():
                    values.append(
                        ReplayStream(
                            symbol,
                            DataType(nested_type),
                            cast(Iterable[MarketEvent] | StreamFactory, nested_events),
                        )
                    )
            else:
                values.append(
                    ReplayStream(
                        symbol,
                        data_type,
                        cast(Iterable[MarketEvent] | StreamFactory, value),
                    )
                )
    else:
        values.extend(streams)
    sources: list[_Source] = []
    for order, stream in enumerate(values):
        if not isinstance(stream, ReplayStream):
            raise TypeError("streams must contain ReplayStream values")
        data_type = None if stream.data_type is None else DataType(stream.data_type)
        sources.append(
            _Source(
                stream.symbol,
                data_type,
                None,
                order,
                f"stream:{order}:{stream.symbol}",
                lambda stream=stream: _stream_events(stream),
            )
        )
    return tuple(sources)


def _separate_streams(
    quote_streams: Mapping[str, object] | None,
    trade_streams: Mapping[str, object] | None,
) -> tuple[ReplayStream, ...]:
    values: list[ReplayStream] = []
    for symbol, events in (quote_streams or {}).items():
        values.append(
            ReplayStream(
                symbol,
                DataType.QUOTE,
                cast(Iterable[MarketEvent] | StreamFactory, events),
            )
        )
    for symbol, events in (trade_streams or {}).items():
        values.append(
            ReplayStream(
                symbol,
                DataType.TRADE,
                cast(Iterable[MarketEvent] | StreamFactory, events),
            )
        )
    return tuple(values)


def _stream_key(key: object) -> tuple[str, DataType | None]:
    if isinstance(key, tuple):
        if len(key) != 2:
            raise ValueError("stream tuple keys must be (symbol, data_type)")
        if not isinstance(key[0], str):
            raise TypeError("stream symbol keys must be strings")
        return _canonical_symbol(key[0]), DataType(key[1])
    if not isinstance(key, str):
        raise TypeError("stream symbol keys must be strings")
    return _canonical_symbol(key), None


def _stream_events(stream: ReplayStream) -> Iterable[MarketEvent]:
    for event in stream.open():
        yield _require_event(event)


def _normalize_files(files: Iterable[FileQueryFile]) -> tuple[FileQueryFile, ...]:
    values = tuple(files)
    for file in values:
        if not isinstance(file, FileQueryFile):
            raise TypeError("files must contain FileQueryFile values")
        if file.data_type not in {DataType.QUOTE, DataType.TRADE}:
            raise ValueError("replay files must be quote or trade partitions")
        if file.row_count < 0 or file.byte_size < 0:
            raise ValueError("file row_count and byte_size must be non-negative")
        expected_path = partition_path(
            session_date=file.session_date,
            asset_class=LakeAssetClass(file.asset_class.value),
            data_type=LakeDataType(file.data_type.value),
            symbol=file.symbol,
        )
        if file.object_path != expected_path:
            raise ValueError("replay file has a non-canonical lake partition path")
    return tuple(
        sorted(
            values,
            key=lambda file: (
                file.session_date,
                _canonical_symbol(file.symbol),
                file.data_type.value,
                file.object_path,
            ),
        )
    )


def _validate_file_sessions(
    files: Sequence[FileQueryFile], sessions: Sequence[ExchangeSession]
) -> None:
    expected_dates = {session.session_date for session in sessions}
    seen: set[tuple[date, str, DataType]] = set()
    for file in files:
        if file.session_date not in expected_dates:
            raise ValueError("replay file session is not part of the replay")
        key = (file.session_date, _canonical_symbol(file.symbol), file.data_type)
        if key in seen:
            raise ValueError(
                "replay files must not duplicate a symbol/data-type session"
            )
        seen.add(key)


def _reader_events(output: object, file: FileQueryFile) -> Iterable[MarketEvent]:
    """Adapt injected event, row, Arrow, path, or codec output lazily."""
    if isinstance(output, Event):
        yield _require_event(output)
        return
    if isinstance(output, bytes):
        yield from _parquet_bytes_events(output, file)
        return
    if isinstance(output, Path | str):
        yield from _path_events(Path(output), file)
        return

    batch_reader = getattr(output, "read_next_batch", None)
    if callable(batch_reader):
        while True:
            try:
                batch = batch_reader()
            except StopIteration:
                return
            if batch is None:
                return
            yield from _rows_events(_batch_rows(batch), file)
        return

    to_batches = getattr(output, "to_batches", None)
    if callable(to_batches):
        for batch in to_batches():
            yield from _rows_events(_batch_rows(batch), file)
        return

    if isinstance(output, Mapping):
        yield _row_event(output, file, 0)
        return
    try:
        iterator = iter(cast(Iterable[object], output))
    except TypeError as error:
        raise TypeError(
            "reader must return an event or iterable reader output"
        ) from error
    for row_number, value in enumerate(iterator):
        if isinstance(value, Event):
            yield _require_event(value)
        elif isinstance(value, Mapping):
            yield _row_event(value, file, row_number)
        else:
            raise TypeError("reader iterables must contain events or mapping rows")


def _path_events(path: Path, file: FileQueryFile) -> Iterable[MarketEvent]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:  # pragma: no cover - pyarrow is a project dependency
        raise RuntimeError(
            "pyarrow is required for path-based replay readers"
        ) from error
    parquet_file = parquet.ParquetFile(path)
    for batch in parquet_file.iter_batches():
        yield from _rows_events(_batch_rows(batch), file)


def _parquet_bytes_events(payload: bytes, file: FileQueryFile) -> Iterable[MarketEvent]:
    """Read a byte payload in batches so one file is not expanded at once."""
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:  # pragma: no cover - pyarrow is a project dependency
        raise RuntimeError(
            "pyarrow is required for byte-based replay readers"
        ) from error
    parquet_file = parquet.ParquetFile(io.BytesIO(payload))
    for batch in parquet_file.iter_batches():
        yield from _rows_events(_batch_rows(batch), file)


def _batch_rows(batch: object) -> Iterable[Mapping[str, object] | MarketEvent]:
    to_pylist = getattr(batch, "to_pylist", None)
    if not callable(to_pylist):
        raise TypeError("reader batches must provide to_pylist")
    return cast(Iterable[Mapping[str, object] | MarketEvent], to_pylist())


def _rows_events(
    rows: Iterable[Mapping[str, object] | MarketEvent], file: FileQueryFile
) -> Iterable[MarketEvent]:
    for row_number, row in enumerate(rows):
        if isinstance(row, Event):
            yield _require_event(row)
        else:
            yield _row_event(row, file, row_number)


def _row_event(
    row: Mapping[str, object], file: FileQueryFile, row_number: int
) -> MarketEvent:
    data_type = file.data_type
    timestamp_value = row.get("timestamp")
    if isinstance(timestamp_value, datetime):
        timestamp = timestamp_value
    elif isinstance(timestamp_value, str):
        timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
    else:
        raise TypeError("Parquet replay rows must contain datetime timestamps")
    symbol = canonical_symbol(str(row.get("symbol", file.symbol)))
    event_id = str(row.get("event_id", f"{file.object_path}#{row_number}"))
    raw_sequence = row.get("sequence")
    if raw_sequence is None:
        raw_sequence = row.get("trade_id", row_number)
    sequence = _integer_sequence(raw_sequence, row_number)
    if data_type is DataType.QUOTE:
        venue = row.get("venue", row.get("bid_exchange", row.get("ask_exchange")))
        return QuoteEvent(
            event_id,
            timestamp,
            sequence,
            symbol,
            _decimal_value(row["bid_price"]),
            _decimal_value(row.get("bid_quantity", row.get("bid_size"))),
            _decimal_value(row["ask_price"]),
            _decimal_value(row.get("ask_quantity", row.get("ask_size"))),
            None if venue is None else str(venue),
        )
    venue = row.get("venue", row.get("exchange"))
    trade_id = row.get("trade_id")
    return TradeEvent(
        event_id,
        timestamp,
        sequence,
        symbol,
        _decimal_value(row["price"]),
        _decimal_value(row.get("quantity", row.get("size"))),
        None if venue is None else str(venue),
        None if trade_id is None else str(trade_id),
    )


def _validate_source_event(event: MarketEvent, source: _Source) -> None:
    if event.symbol != source.symbol:
        raise ValueError(
            f"replay event symbol {event.symbol!s} does not match source {source.symbol!r}"
        )
    expected = (
        EventType.QUOTE
        if source.data_type is DataType.QUOTE
        else EventType.TRADE
        if source.data_type is DataType.TRADE
        else None
    )
    if expected is not None and event.event_type is not expected:
        raise ValueError("replay event type does not match its source data type")


def _require_event(value: object) -> MarketEvent:
    if not isinstance(value, (QuoteEvent, TradeEvent)):
        raise TypeError("replay sources must contain QuoteEvent or TradeEvent values")
    return value


def _canonical_symbol(value: str) -> str:
    return str(canonical_symbol(value))


def _decimal_value(value: object) -> Decimal:
    return exact_decimal(value, "Parquet decimal field")


def _integer_sequence(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback


def _file_fingerprint(
    sources: Sequence[_Source], files: Sequence[FileQueryFile]
) -> str:
    digest = hashlib.sha256()
    digest.update(b"fca-lake-replay-files-v1\0")
    for source in sources:
        if source.session_date is not None:
            continue
        _update_frame(
            digest,
            _canonical_fields(
                ("stream", source.order, source.symbol, source.data_type)
            ),
        )
    for file in files:
        _update_frame(
            digest,
            _canonical_fields(
                (
                    "file",
                    file.session_date.isoformat(),
                    _canonical_symbol(file.symbol),
                    file.data_type.value,
                    file.object_path,
                    file.row_count,
                    file.byte_size,
                    file.checksum or "",
                )
            ),
        )
    return digest.hexdigest()


def _event_fingerprint_bytes(event: MarketEvent) -> bytes:
    # Use the same exact decimal and event fields as ParquetCodec, without
    # allocating a one-row Parquet file for every event in a large replay.
    if isinstance(event, QuoteEvent):
        values: tuple[object, ...] = (
            "quote",
            event.event_id,
            event.timestamp.isoformat(timespec="microseconds"),
            event.sequence,
            str(event.symbol),
            str(event.bid_price),
            str(event.bid_quantity),
            str(event.ask_price),
            str(event.ask_quantity),
            event.venue or "",
        )
    else:
        values = (
            "trade",
            event.event_id,
            event.timestamp.isoformat(timespec="microseconds"),
            event.sequence,
            str(event.symbol),
            str(event.price),
            str(event.quantity),
            event.venue or "",
            event.trade_id or "",
        )
    # schema_for validates that only event types supported by the canonical
    # Parquet codec reach the fingerprint boundary.
    ParquetCodec.schema_for(type(event))
    return _canonical_fields(values)


def _canonical_fields(values: Iterable[object]) -> bytes:
    output = bytearray()
    for value in values:
        payload = str(value).encode("utf-8")
        output.extend(len(payload).to_bytes(8, "big"))
        output.extend(payload)
    return bytes(output)


def _update_frame(digest: Any, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _empty_data_fingerprint(
    file_fingerprint: str, sessions: Sequence[ExchangeSession] = ()
) -> str:
    state = _FingerprintState(file_fingerprint, sessions)
    return state.data.hexdigest()


__all__ = [
    "LakeReplayFeed",
    "MarketEvent",
    "ParquetReader",
    "ReplayFingerprints",
    "ReplayStream",
]
