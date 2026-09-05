"""Day-bounded, deterministic replay of historical option market data.

Options replay navigates the market-lake ``date/asset_class/data_type``
partition layout for option quote and trade files and streams each session
through the existing bounded replay contract, :class:`LakeReplayFeed`.

Contract selection is point in time: every session resolves its selectable
contracts through an injected
:class:`~harness.data.options_universe.OptionUniverseView`, so front-set
membership rolls exactly when the catalog rolled, static OSI members must
prove catalog visibility on the session date, and no catalog row dated after
the engine clock head is ever consulted.  Planning a session newer than the
universe clock head raises :class:`~harness.data.history.LookaheadError`
before any partition is located.

The replay plans and opens one session at a time.  Only that session's
partitions are located through the injected file port and activated through
the injected reader, so a two-year front-set run holds at most one day of
event lookahead at a time regardless of range length.  Like the bounded
replay it wraps, no code path here reads wall time, consumes randomness, or
touches the network: the file port and reader are the only injected read
boundaries.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from harness.core.symbols import canonical_symbol
from harness.data.options_universe import OptionUniverseView
from harness.data.replay import (
    LakeReplayFeed,
    MarketEvent,
    ParquetReader,
    ReplayFingerprints,
)
from lake.catalog.models import ContractRecord
from lake.contracts import ExchangeSession
from lakeclient import AssetClass, DataType, FileQueryFile


@runtime_checkable
class OptionPartitionPort(Protocol):
    """Read boundary over the market-lake option partition layout.

    Implementations answer one canonical
    ``date=.../asset_class=option/data_type=.../<symbol>.parquet`` partition
    per contract session without materializing anything else.  The port is
    consulted only for the session currently being planned.
    """

    def file(
        self, *, session_date: date, data_type: DataType, symbol: str
    ) -> FileQueryFile | None:
        """Return the option partition for one contract session, if present."""
        ...


@dataclass(frozen=True, slots=True)
class OptionsDayPlan:
    """One resolved session: selected contracts, partitions, and its feed.

    ``contracts`` is the point-in-time contract snapshot for the session
    date, ``files`` are the located option partitions in deterministic order,
    and ``feed`` is the :class:`LakeReplayFeed` that owns ordering,
    sessioning, and fingerprinting for the day.
    """

    session: ExchangeSession
    contracts: tuple[ContractRecord, ...]
    files: tuple[FileQueryFile, ...]
    feed: LakeReplayFeed

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return the selected OSI symbols in deterministic order."""
        return tuple(record.symbol for record in self.contracts)


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


def _normalize_data_types(
    values: Iterable[DataType | str],
) -> tuple[DataType, ...]:
    normalized = sorted(
        {DataType(value) for value in values}, key=lambda value: value.value
    )
    if not normalized:
        raise ValueError("at least one option replay data type is required")
    for value in normalized:
        if value not in (DataType.QUOTE, DataType.TRADE):
            raise ValueError("option replay data types must be quote or trade")
    return tuple(normalized)


def _frame(values: Iterable[object]) -> bytes:
    payload = bytearray()
    for value in values:
        encoded = str(value).encode("utf-8")
        payload.extend(len(encoded).to_bytes(8, "big"))
        payload.extend(encoded)
    return bytes(payload)


def _update_frame(digest: Any, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _new_run_digests(
    frames: tuple[bytes, ...],
) -> tuple[Any, Any, Any]:
    digests = (hashlib.sha256(), hashlib.sha256(), hashlib.sha256())
    for digest in digests:
        for frame in frames:
            _update_frame(digest, frame)
    return digests


class OptionsReplayFeed:
    """Stream deterministic option events one explicit session at a time.

    Each session is planned lazily: the point-in-time contract snapshot for
    the session date is resolved through the injected universe view, the
    session's option partitions are located through the injected
    :class:`OptionPartitionPort`, and the day is merged by
    :class:`LakeReplayFeed` together with any injected quote and trade
    streams (for example the underlying equity).  Because streams are
    re-opened once per planned day, callers replaying multiple days should
    supply zero-argument factories rather than one-shot iterators.

    ``fingerprints`` chain the per-day feed fingerprints in session order and
    describe the most recent completed run; before any run they equal the
    deterministic header-only baseline.
    """

    __slots__ = (
        "_data_types",
        "_files",
        "_last_fingerprints",
        "_quote_streams",
        "_reader",
        "_sessions",
        "_trade_streams",
        "_universe",
    )

    def __init__(
        self,
        sessions: Iterable[ExchangeSession | date],
        universe: OptionUniverseView,
        files: OptionPartitionPort,
        *,
        reader: ParquetReader,
        data_types: Iterable[DataType | str] = (DataType.QUOTE, DataType.TRADE),
        quote_streams: Mapping[str, object] | None = None,
        trade_streams: Mapping[str, object] | None = None,
    ) -> None:
        self._sessions = _normalize_sessions(sessions)
        if not isinstance(universe, OptionUniverseView):
            raise TypeError("universe must be an OptionUniverseView")
        if not isinstance(files, OptionPartitionPort):
            raise TypeError("files must implement OptionPartitionPort")
        if reader is None or not callable(reader):
            raise TypeError("reader is required and must be callable")
        if quote_streams is not None and not isinstance(quote_streams, Mapping):
            raise TypeError("quote_streams must be a mapping")
        if trade_streams is not None and not isinstance(trade_streams, Mapping):
            raise TypeError("trade_streams must be a mapping")
        self._universe = universe
        self._files = files
        self._reader = reader
        self._data_types = _normalize_data_types(data_types)
        self._quote_streams = quote_streams
        self._trade_streams = trade_streams
        self._last_fingerprints = self._baseline_fingerprints()

    @property
    def sessions(self) -> tuple[ExchangeSession, ...]:
        """Return the explicit replay sessions in chronological order."""
        return self._sessions

    @property
    def universe(self) -> OptionUniverseView:
        """Return the injected point-in-time contract universe."""
        return self._universe

    @property
    def data_types(self) -> tuple[DataType, ...]:
        """Return the replayed option data types in deterministic order."""
        return self._data_types

    @property
    def fingerprints(self) -> ReplayFingerprints:
        """Return the fingerprints of the most recent completed run."""
        return self._last_fingerprints

    @property
    def file_fingerprint(self) -> str:
        """Return the latest selected-partition fingerprint."""
        return self._last_fingerprints.file

    @property
    def row_fingerprint(self) -> str:
        """Return the latest ordered-row fingerprint."""
        return self._last_fingerprints.row

    @property
    def data_fingerprint(self) -> str:
        """Return the latest complete-replay fingerprint."""
        return self._last_fingerprints.data

    def plan(self, session: ExchangeSession | date) -> OptionsDayPlan:
        """Resolve one explicit session into contracts, files, and a feed."""
        selected = _normalize_sessions((session,))[0]
        if selected not in self._sessions:
            raise ValueError("session is not part of this options replay")
        return self._plan(selected)

    def days(self) -> Iterator[OptionsDayPlan]:
        """Plan and yield every session lazily, in session order."""
        for session in self._sessions:
            yield self._plan(session)

    def __iter__(self) -> Iterator[MarketEvent]:
        """Yield the merged events of every session in chronological order."""
        file_digest, row_digest, data_digest = _new_run_digests(self._header_frames())
        for session in self._sessions:
            plan = self._plan(session)
            _update_frame(
                file_digest,
                _frame((session.session_date.isoformat(), plan.feed.file_fingerprint)),
            )
            yield from plan.feed
            fingerprints = plan.feed.fingerprints
            _update_frame(
                row_digest,
                _frame(
                    (session.session_date.isoformat(), fingerprints.row_fingerprint)
                ),
            )
            _update_frame(
                data_digest,
                _frame(
                    (session.session_date.isoformat(), fingerprints.data_fingerprint)
                ),
            )
        self._last_fingerprints = ReplayFingerprints(
            file_digest.hexdigest(),
            row_digest.hexdigest(),
            data_digest.hexdigest(),
        )

    def _plan(self, session: ExchangeSession) -> OptionsDayPlan:
        snapshot = self._universe.resolve(session.open_time)
        files: list[FileQueryFile] = []
        for symbol in snapshot.symbols:
            for data_type in self._data_types:
                file = self._files.file(
                    session_date=session.session_date,
                    data_type=data_type,
                    symbol=symbol,
                )
                if file is None:
                    continue
                self._validate_file(file, session, data_type, symbol)
                files.append(file)
        feed = LakeReplayFeed(
            (session,),
            files=tuple(files),
            reader=self._reader,
            quote_streams=self._quote_streams,
            trade_streams=self._trade_streams,
        )
        return OptionsDayPlan(
            session=session,
            contracts=snapshot.contracts,
            files=tuple(files),
            feed=feed,
        )

    @staticmethod
    def _validate_file(
        file: FileQueryFile,
        session: ExchangeSession,
        data_type: DataType,
        symbol: str,
    ) -> None:
        if file.asset_class is not AssetClass.OPTION:
            raise ValueError(
                f"option replay file {file.object_path!r} is not an option partition"
            )
        if file.data_type is not data_type:
            raise ValueError(
                f"option replay file {file.object_path!r} is a "
                f"{file.data_type.value} partition, not {data_type.value}"
            )
        if file.session_date != session.session_date:
            raise ValueError(
                f"option replay file {file.object_path!r} belongs to "
                f"{file.session_date.isoformat()}, not "
                f"{session.session_date.isoformat()}"
            )
        if canonical_symbol(file.symbol) != canonical_symbol(symbol):
            raise ValueError(
                f"option replay file {file.object_path!r} belongs to "
                f"{file.symbol!r}, not {symbol!r}"
            )
        if file.row_count < 0 or file.byte_size < 0:
            raise ValueError(
                f"option replay file {file.object_path!r} has negative "
                "row_count or byte_size"
            )

    def _header_frames(self) -> tuple[bytes, ...]:
        spec = self._universe.spec
        return (
            _frame(
                (
                    "fca-options-replay-run-v1",
                    str(spec.underlying),
                    spec.mode.value,
                    *spec.static_symbols,
                )
            ),
            _frame(tuple(data_type.value for data_type in self._data_types)),
            _frame(
                tuple(session.session_date.isoformat() for session in self._sessions)
            ),
        )

    def _baseline_fingerprints(self) -> ReplayFingerprints:
        file_digest, row_digest, data_digest = _new_run_digests(self._header_frames())
        return ReplayFingerprints(
            file_digest.hexdigest(),
            row_digest.hexdigest(),
            data_digest.hexdigest(),
        )


__all__ = [
    "OptionPartitionPort",
    "OptionsDayPlan",
    "OptionsReplayFeed",
]
