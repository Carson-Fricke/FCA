"""Deterministic healing of live-feed gaps through the market lake.

A live feed can drop messages: a provider outage, a stalled websocket, or a
silence that outlasts the staleness threshold.  :class:`LiveGapHealer` sits on
the seam between that feed and the engine.  It classifies silences against the
calendar-aware expected coverage resolved from the lake session contract,
marks affected intervals dirty, ensures the lake session that owns the hole
through the published lakeclient port (polling accepted ensure jobs), replays
the missing slice from the returned partitions, and only then lets live flow
resume.  Every heal attempt appends an immutable :class:`HealingAction` to the
audit trail, so the trail carries the exact interval, the sessions touched,
and the outcome.

Seam ownership is explicit, so healing never reorders or duplicates events:

* the live stream owns the boundary moments of a gap -- the last event before
  the silence and the event that resumes flow -- so the backfilled slice is
  strictly inside the half-open gap interval ``(gap.start, gap.end)``;
* the lake is authoritative inside a healed interval, so a late live event
  landing inside an already-healed window is dropped and counted, never
  emitted out of order;
* healed events are emitted in timestamp order per ``(symbol, data_type)``
  partition, and a heal that would have to reorder live flow is refused.

All time comes from event timestamps, an explicit ``now``, or an injected
:class:`~harness.core.clock.Clock`; storage access goes through the same
published lakeclient port the market-lake adapter uses.  When the lake cannot
cover a hole -- missing, erroring, or uncovered sessions -- the attempt is
recorded as uncovered and :class:`UncoveredGapError` names the interval.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from harness.core.clock import Clock
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.common import text, timestamp
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.data.lake import (
    EnsureJobError,
    LakeClientPort,
    MarketLakeError,
    UnsupportedMarketDataError,
)
from harness.data.replay import LakeReplayFeed, ParquetReader
from lake.contracts import (
    AssetClass as LakeContractAssetClass,
)
from lake.contracts import (
    DataType as LakeContractDataType,
)
from lake.contracts import (
    ExchangeSession,
    crypto_utc_days,
    partition_path,
    xnys_sessions,
)
from lakeclient import (
    AssetClass,
    CoverageEntry,
    CoverageResponse,
    CoverageState,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    FileQueryFile,
    JobPollingTimeout,
    LakeClientError,
)

type MarketEvent = QuoteEvent | TradeEvent

CoveragePartition = tuple[str, DataType]
HealedWindows = tuple[tuple[datetime, datetime], ...]

_ASSET_CLASSES = frozenset({AssetClass.EQUITY, AssetClass.CRYPTO})
_COVERED_STATES = frozenset({CoverageState.COMPLETE, CoverageState.PARTIAL})
_SESSION_PADDING = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """One hole in a live feed's expected coverage, half-open in time.

    ``start`` is the last live-delivered moment (exclusive in the healed
    slice) and ``end`` is the moment live flow resumes (also exclusive), so
    the boundary events stay owned by the live stream.
    """

    symbol: str
    data_type: DataType
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "symbol",
            str(canonical_symbol(text(self.symbol, "symbol"))),
        )
        object.__setattr__(self, "data_type", DataType(self.data_type))
        start = timestamp(self.start, "start")
        end = timestamp(self.end, "end")
        if start >= end:
            raise ValueError("gap start must precede gap end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def partition(self) -> CoveragePartition:
        """Return the ``(symbol, data_type)`` partition this gap belongs to."""
        return (self.symbol, self.data_type)

    @property
    def duration(self) -> timedelta:
        """Return the half-open interval length of the gap."""
        return self.end - self.start


@runtime_checkable
class CoverageCalendar(Protocol):
    """Expected-coverage windows for one asset class."""

    def sessions(self, start: date, end: date) -> tuple[ExchangeSession, ...]:
        """Return the sessions whose windows overlap the inclusive date range."""
        ...


class LakeSessionCalendar:
    """Expected coverage from the lake session contract, without any clock.

    Equity coverage follows XNYS sessions and crypto coverage follows whole
    UTC days, exactly like the market-lake adapter resolves sessions, so a
    silence outside those windows is never classified as a gap.
    """

    __slots__ = ("_asset_class",)

    def __init__(self, asset_class: AssetClass | str) -> None:
        resolved = AssetClass(asset_class)
        if resolved not in _ASSET_CLASSES:
            raise UnsupportedMarketDataError(
                f"gap healing does not support {resolved.value} data"
            )
        self._asset_class = resolved

    @property
    def asset_class(self) -> AssetClass:
        """Return the asset class whose sessions this calendar resolves."""
        return self._asset_class

    def sessions(self, start: date, end: date) -> tuple[ExchangeSession, ...]:
        """Return XNYS sessions or UTC-day sessions in the inclusive range."""
        if isinstance(start, datetime) or not isinstance(start, date):
            raise TypeError("start must be a date, not a datetime")
        if isinstance(end, datetime) or not isinstance(end, date):
            raise TypeError("end must be a date, not a datetime")
        if end < start:
            raise ValueError("end must not be before start")
        if self._asset_class is AssetClass.EQUITY:
            return xnys_sessions(start, end)
        return tuple(
            ExchangeSession(
                session_date=day,
                open_time=datetime.combine(day, datetime.min.time(), UTC),
                close_time=datetime.combine(day, datetime.min.time(), UTC)
                + timedelta(days=1),
            )
            for day in crypto_utc_days(start, end)
        )


class LiveGapDetector:
    """Classify live-feed silences as gaps against expected coverage.

    The detector only observes event timestamps: it never reads a clock and
    never performs input or output.  Healed or attempted intervals stay dirty
    through their end, so repeated detection cannot loop on the same hole.
    """

    __slots__ = ("_calendar", "_dirty_through", "_last_seen")

    def __init__(self, *, calendar: CoverageCalendar) -> None:
        if not isinstance(calendar, CoverageCalendar):
            raise TypeError("calendar must implement CoverageCalendar")
        self._calendar = calendar
        self._last_seen: dict[CoveragePartition, datetime] = {}
        self._dirty_through: dict[CoveragePartition, datetime] = {}

    @property
    def calendar(self) -> CoverageCalendar:
        """Return the expected-coverage calendar."""
        return self._calendar

    @property
    def tracked(self) -> tuple[CoveragePartition, ...]:
        """Return the tracked partitions in deterministic order."""
        return tuple(sorted(self._last_seen, key=_partition_key))

    def last_seen(self, symbol: str, data_type: DataType | str) -> datetime | None:
        """Return the newest live timestamp observed for one partition."""
        return self._last_seen.get(_partition(symbol, data_type))

    def observe(self, event: MarketEvent) -> datetime:
        """Record one delivered live event and return the partition head."""
        partition = _event_partition(event)
        current = self._last_seen.get(partition)
        if current is None or event.timestamp > current:
            self._last_seen[partition] = event.timestamp
        return self._last_seen[partition]

    def mark_dirty(self, gap: CoverageGap) -> None:
        """Extend the interval that must not be detected as a new gap."""
        current = self._dirty_through.get(gap.partition)
        if current is None or gap.end > current:
            self._dirty_through[gap.partition] = gap.end

    def detect(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
        symbols: Sequence[str] | None = None,
    ) -> tuple[CoverageGap, ...]:
        """Return gaps for tracked partitions silent for at least ``stale_after``."""
        head = timestamp(now, "now")
        _positive_duration(stale_after, "stale_after")
        gaps: list[CoverageGap] = []
        for partition in self._selected(symbols):
            last = self._last_seen.get(partition)
            if last is None or head - last < stale_after:
                continue
            gaps.extend(self._fresh(partition, last, head))
        return tuple(gaps)

    def detect_between(
        self,
        symbol: str,
        data_type: DataType | str,
        start: datetime,
        end: datetime,
    ) -> tuple[CoverageGap, ...]:
        """Return gaps for one partition inside ``[start, end)``, dirty-clipped."""
        partition = _partition(symbol, data_type)
        return self._fresh(partition, timestamp(start, "start"), timestamp(end, "end"))

    def _fresh(
        self, partition: CoveragePartition, start: datetime, end: datetime
    ) -> tuple[CoverageGap, ...]:
        if start >= end:
            return ()
        dirty = self._dirty_through.get(partition)
        if dirty is not None and dirty > start:
            start = dirty
            if start >= end:
                return ()
        symbol, data_type = partition
        return tuple(
            CoverageGap(symbol, data_type, window_start, window_end)
            for window_start, window_end in self._coverage_windows(start, end)
        )

    def _coverage_windows(
        self, start: datetime, end: datetime
    ) -> tuple[tuple[datetime, datetime], ...]:
        sessions = self._calendar.sessions(
            start.date() - _SESSION_PADDING, end.date() + _SESSION_PADDING
        )
        windows: list[tuple[datetime, datetime]] = []
        for session in sessions:
            window_start = max(start, session.open_time)
            window_end = min(end, session.close_time)
            if window_start < window_end:
                windows.append((window_start, window_end))
        return tuple(windows)

    def _selected(self, symbols: Sequence[str] | None) -> tuple[CoveragePartition, ...]:
        tracked = self.tracked
        if symbols is None:
            return tracked
        wanted = {str(Symbol(value)) for value in symbols}
        return tuple(partition for partition in tracked if partition[0] in wanted)


class HealingStatus(StrEnum):
    """Outcome of one heal attempt recorded in the action trail."""

    HEALED = "healed"
    UNCOVERED = "uncovered"


@dataclass(frozen=True, slots=True)
class HealingAction:
    """Immutable audit record for one heal attempt over one gap interval."""

    gap: CoverageGap
    status: HealingStatus
    events: int
    sessions: tuple[date, ...]
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", HealingStatus(self.status))
        if isinstance(self.events, bool) or not isinstance(self.events, int):
            raise TypeError("events must be an integer")
        if self.events < 0:
            raise ValueError("events must not be negative")
        for value in self.sessions:
            if isinstance(value, datetime) or not isinstance(value, date):
                raise TypeError("sessions must contain date values")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")


class UncoveredGapError(MarketLakeError):
    """The market lake could not cover a detected gap without reordering.

    ``gap`` names the interval that could not be healed, ``reason`` carries
    the lake's own account of why, and ``healed_events`` holds any events
    healed for earlier gaps in the same attempt so callers can publish them
    before handling the failure.
    """

    def __init__(
        self,
        gap: CoverageGap,
        reason: str,
        healed_events: tuple[MarketEvent, ...] = (),
    ) -> None:
        self.gap = gap
        self.reason = reason
        self.healed_events = healed_events
        super().__init__(
            f"{gap.symbol} {gap.data_type.value} "
            f"[{gap.start.isoformat()}, {gap.end.isoformat()}): {reason}"
        )


class LakeGapBackfiller:
    """Fetch one gap's missing slice from the lake through ensure and query.

    The backfiller is bounded by the lake's own behavior: a session is only
    covered when the lake reports it complete or partial (the current live
    session), or authoritatively empty, and only files the lake query returns
    are read through the injected reader.  A hole the lake cannot cover
    raises :class:`UncoveredGapError` naming the session and the reason.
    """

    __slots__ = (
        "_asset_class",
        "_calendar",
        "_client",
        "_job_poll_interval",
        "_job_timeout",
        "_reader",
    )

    def __init__(
        self,
        *,
        client: LakeClientPort,
        reader: ParquetReader,
        calendar: CoverageCalendar,
        asset_class: AssetClass | str,
        job_timeout: float = 60.0,
        job_poll_interval: float = 0.5,
    ) -> None:
        for name in ("ensure", "wait_for_job", "query_files"):
            if not callable(getattr(client, name, None)):
                raise TypeError(
                    f"client must implement LakeClientPort ({name} is missing)"
                )
        if not callable(reader):
            raise TypeError("reader must be callable")
        if not isinstance(calendar, CoverageCalendar):
            raise TypeError("calendar must implement CoverageCalendar")
        resolved = AssetClass(asset_class)
        if resolved not in _ASSET_CLASSES:
            raise UnsupportedMarketDataError(
                f"gap healing does not support {resolved.value} data"
            )
        if isinstance(job_timeout, bool) or job_timeout < 0:
            raise ValueError("job_timeout must not be negative")
        if isinstance(job_poll_interval, bool) or job_poll_interval < 0:
            raise ValueError("job_poll_interval must not be negative")
        self._client = client
        self._reader = reader
        self._calendar = calendar
        self._asset_class = resolved
        self._job_timeout = job_timeout
        self._job_poll_interval = job_poll_interval

    @property
    def asset_class(self) -> AssetClass:
        """Return the asset class requested from the lake."""
        return self._asset_class

    @property
    def calendar(self) -> CoverageCalendar:
        """Return the expected-coverage calendar."""
        return self._calendar

    def backfill(self, gap: CoverageGap) -> tuple[MarketEvent, ...]:
        """Return the gap's missing events in timestamp order.

        The slice is strictly inside ``(gap.start, gap.end)``: the boundary
        events at both ends stay owned by the live stream, so nothing is
        duplicated at the historical-to-live seam.
        """
        sessions = self._expected_sessions(gap)
        request = DatasetRequest(
            asset_class=self._asset_class,
            data_type=gap.data_type,
            symbol=gap.symbol,
            start_date=sessions[0].session_date,
            end_date=sessions[-1].session_date,
        )
        coverage = self._ensure(
            request, frozenset(session.session_date for session in sessions)
        )
        covered = self._covered_sessions(gap, coverage, sessions)
        files = self._query(gap, request, covered, sessions)
        return self._read(gap, sessions, files)

    def _expected_sessions(self, gap: CoverageGap) -> tuple[ExchangeSession, ...]:
        candidates = self._calendar.sessions(
            gap.start.date() - _SESSION_PADDING, gap.end.date() + _SESSION_PADDING
        )
        overlapping = tuple(
            sorted(
                (
                    session
                    for session in candidates
                    if session.open_time < gap.end and session.close_time > gap.start
                ),
                key=lambda session: session.session_date,
            )
        )
        if not overlapping:
            raise UncoveredGapError(gap, "the gap overlaps no expected lake session")
        cursor = gap.start
        for session in overlapping:
            if session.open_time > cursor:
                raise UncoveredGapError(
                    gap,
                    "the gap extends outside expected coverage at "
                    f"{session.session_date.isoformat()}",
                )
            if session.close_time > cursor:
                cursor = session.close_time
            if cursor >= gap.end:
                return overlapping
        raise UncoveredGapError(
            gap, "the gap extends past the last expected session close"
        )

    def _ensure(
        self, request: DatasetRequest, expected_dates: frozenset[date]
    ) -> CoverageResponse | None:
        response = self._client.ensure(request)
        if isinstance(response, EnsureAccepted):
            status = self._client.wait_for_job(
                response.job_id,
                timeout=self._job_timeout,
                poll_interval=self._job_poll_interval,
            )
            if not status.succeeded:
                raise EnsureJobError(
                    f"market-lake ensure job {response.job_id!r} failed: "
                    f"{status.error_message or status.state.value}"
                )
            return None
        if isinstance(response, CoverageResponse):
            self._validate_coverage(response, request, expected_dates)
            return response
        raise MarketLakeError("lakeclient returned an invalid ensure response")

    def _validate_coverage(
        self,
        response: CoverageResponse,
        request: DatasetRequest,
        expected_dates: frozenset[date],
    ) -> None:
        if (
            response.asset_class is not request.asset_class
            or response.data_type is not request.data_type
            or response.start_date != request.start_date
            or response.end_date != request.end_date
        ):
            raise MarketLakeError(
                "lakeclient ensure response does not match the request"
            )
        if response.symbol is None or response.symbol.strip().upper() != request.symbol:
            raise MarketLakeError("lakeclient ensure response has an unexpected symbol")
        seen: set[date] = set()
        for entry in response.entries:
            if entry.session_date not in expected_dates or entry.session_date in seen:
                raise MarketLakeError(
                    "lakeclient ensure response has an unexpected or duplicate session"
                )
            seen.add(entry.session_date)

    def _covered_sessions(
        self,
        gap: CoverageGap,
        coverage: CoverageResponse | None,
        sessions: tuple[ExchangeSession, ...],
    ) -> tuple[ExchangeSession, ...] | None:
        """Classify the gap's sessions from the ensure response.

        Returns ``None`` when the lake accepted an asynchronous ensure job:
        without per-session coverage entries, session coverage is decided by
        the files the subsequent query returns.
        """
        if coverage is None:
            return None
        entries: dict[date, CoverageEntry] = {
            entry.session_date: entry for entry in coverage.entries
        }
        covered: list[ExchangeSession] = []
        failures: list[str] = []
        for session in sessions:
            entry = entries.get(session.session_date)
            if entry is None:
                detail = (
                    "the lake returned no partition"
                    if coverage is None
                    else "ensure omitted the session"
                )
                failures.append(f"{session.session_date.isoformat()}: {detail}")
                continue
            if entry.status is CoverageState.EMPTY:
                continue
            if entry.status not in _COVERED_STATES:
                failures.append(
                    f"{session.session_date.isoformat()}: coverage is "
                    f"{entry.error_message or entry.status.value}"
                )
                continue
            if not entry.object_path:
                failures.append(
                    f"{session.session_date.isoformat()}: coverage has no object path"
                )
                continue
            covered.append(session)
        if failures:
            raise UncoveredGapError(gap, "; ".join(failures))
        return tuple(covered)

    def _query(
        self,
        gap: CoverageGap,
        request: DatasetRequest,
        covered: tuple[ExchangeSession, ...] | None,
        sessions: tuple[ExchangeSession, ...],
    ) -> tuple[FileQueryFile, ...]:
        required = (
            {session.session_date for session in sessions}
            if covered is None
            else {session.session_date for session in covered}
        )
        if not required:
            return ()
        response = self._client.query_files(request, ensure=False)
        files: dict[date, FileQueryFile] = {}
        for file in response.files:
            _validate_file(file, request)
            if file.session_date in files:
                raise MarketLakeError(
                    "file query returned a duplicate lake partition for "
                    f"{request.symbol} on {file.session_date.isoformat()}"
                )
            files[file.session_date] = file
        missing = sorted(required.difference(files))
        if missing:
            raise UncoveredGapError(
                gap,
                "; ".join(
                    f"{value.isoformat()}: the lake returned no file for covered "
                    "session"
                    for value in missing
                ),
            )
        return tuple(
            sorted(
                (files[session_date] for session_date in required),
                key=lambda file: (file.session_date, file.object_path),
            )
        )

    def _read(
        self,
        gap: CoverageGap,
        sessions: tuple[ExchangeSession, ...],
        files: tuple[FileQueryFile, ...],
    ) -> tuple[MarketEvent, ...]:
        if not files:
            return ()
        dates = {file.session_date for file in files}
        feed_sessions = tuple(
            session for session in sessions if session.session_date in dates
        )
        feed = LakeReplayFeed(feed_sessions, files=files, reader=self._reader)
        start = gap.start
        end = gap.end
        return tuple(event for event in feed if start < event.timestamp < end)


class LiveGapHealer:
    """Mediate the seam between a live feed and market-lake backfill.

    Feed live events through :meth:`feed`: when an event reveals a silence of
    at least ``stale_after`` for its partition, the healer first replays the
    missing slice from the lake and only then emits the resuming event.
    :meth:`tick` heals silences detected without a resuming event, using the
    injected clock or an explicit ``now``.  Both paths mark each attempted
    interval dirty, append one :class:`HealingAction` per gap, and never emit
    an event out of order: events that would reorder or duplicate the healed
    timeline are dropped and counted.
    """

    __slots__ = (
        "_actions",
        "_backfill",
        "_clock",
        "_detector",
        "_duplicate_drops",
        "_emitted_through",
        "_healed_windows",
        "_stale_after",
        "_unordered_drops",
    )

    def __init__(
        self,
        *,
        detector: LiveGapDetector,
        backfill: LakeGapBackfiller,
        stale_after: timedelta,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(detector, LiveGapDetector):
            raise TypeError("detector must be a LiveGapDetector")
        if not isinstance(backfill, LakeGapBackfiller):
            raise TypeError("backfill must be a LakeGapBackfiller")
        _positive_duration(stale_after, "stale_after")
        if clock is not None and not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        self._detector = detector
        self._backfill = backfill
        self._stale_after = stale_after
        self._clock = clock
        self._actions: list[HealingAction] = []
        self._emitted_through: dict[CoveragePartition, datetime] = {}
        self._healed_windows: dict[CoveragePartition, HealedWindows] = {}
        self._duplicate_drops = 0
        self._unordered_drops = 0

    @property
    def detector(self) -> LiveGapDetector:
        """Return the configured gap detector."""
        return self._detector

    @property
    def stale_after(self) -> timedelta:
        """Return the silence duration that classifies a partition as stale."""
        return self._stale_after

    @property
    def actions(self) -> tuple[HealingAction, ...]:
        """Return the immutable heal-attempt trail in attempt order."""
        return tuple(self._actions)

    @property
    def duplicate_events_dropped(self) -> int:
        """Return live events dropped because they landed inside a healed interval."""
        return self._duplicate_drops

    @property
    def unordered_events_dropped(self) -> int:
        """Return events dropped because they preceded the healed timeline."""
        return self._unordered_drops

    def feed(self, event: MarketEvent) -> tuple[MarketEvent, ...]:
        """Emit one live event, healing a revealed gap before resuming flow.

        The returned tuple holds backfilled events first (when the event
        resumes a silent partition) and the event itself last; publish it in
        order.  An event that would reorder or duplicate the healed timeline
        is dropped and counted instead.
        """
        partition = _event_partition(event)
        healed: tuple[MarketEvent, ...] = ()
        last = self._detector.last_seen(*partition)
        if last is not None and event.timestamp - last >= self._stale_after:
            healed = self.heal(
                self._detector.detect_between(
                    partition[0], partition[1], last, event.timestamp
                )
            )
        if self._should_drop(event, partition):
            return healed
        self._detector.observe(event)
        through = self._emitted_through.get(partition)
        if through is None or event.timestamp > through:
            self._emitted_through[partition] = event.timestamp
        return (*healed, event)

    def tick(self, now: datetime | None = None) -> tuple[MarketEvent, ...]:
        """Detect and heal staleness for every tracked partition.

        Call this while the feed is silent, with an explicit ``now`` or with
        the injected clock's head.  The returned events are the healed
        backfill slices in gap order.
        """
        return self.heal(
            self._detector.detect(
                now=self._resolve_now(now), stale_after=self._stale_after
            )
        )

    def heal(self, gaps: Sequence[CoverageGap]) -> tuple[MarketEvent, ...]:
        """Backfill the given gaps in deterministic order and emit the slice.

        Gaps are processed ordered by symbol, data type, and start.  Every
        attempt marks its interval dirty and appends one
        :class:`HealingAction` before returning or raising, and
        :class:`UncoveredGapError` carries the events healed for earlier gaps
        in the same call.
        """
        collected: list[MarketEvent] = []
        for gap in sorted(
            gaps, key=lambda value: (value.symbol, value.data_type.value, value.start)
        ):
            self._detector.mark_dirty(gap)
            through = self._emitted_through.get(gap.partition)
            if through is not None and gap.end <= through:
                reason = "live flow already passed the end of the gap"
                self._actions.append(
                    HealingAction(gap, HealingStatus.UNCOVERED, 0, (), reason)
                )
                raise UncoveredGapError(gap, reason, tuple(collected))
            try:
                healed = self._backfill.backfill(gap)
            except (MarketLakeError, LakeClientError, JobPollingTimeout) as error:
                reason = (
                    error.reason
                    if isinstance(error, UncoveredGapError)
                    else _reason_text(error)
                )
                self._actions.append(
                    HealingAction(gap, HealingStatus.UNCOVERED, 0, (), reason)
                )
                raise UncoveredGapError(gap, reason, tuple(collected)) from error
            admitted = self._admit(gap, healed)
            self._actions.append(
                HealingAction(
                    gap,
                    HealingStatus.HEALED,
                    len(admitted),
                    _overlap_dates(self._detector.calendar, gap),
                    _healed_detail(len(admitted), len(healed)),
                )
            )
            collected.extend(admitted)
        return tuple(collected)

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return timestamp(now, "now")
        if self._clock is not None:
            return timestamp(self._clock.now(), "clock.now")
        raise TypeError("now is required when the healer has no clock")

    def _should_drop(self, event: MarketEvent, partition: CoveragePartition) -> bool:
        moment = event.timestamp
        for window_start, window_end in self._healed_windows.get(partition, ()):
            if window_start < moment < window_end:
                self._duplicate_drops += 1
                return True
        through = self._emitted_through.get(partition)
        if through is not None and moment < through:
            self._unordered_drops += 1
            return True
        return False

    def _admit(
        self, gap: CoverageGap, healed: tuple[MarketEvent, ...]
    ) -> tuple[MarketEvent, ...]:
        partition = gap.partition
        admitted: list[MarketEvent] = []
        for event in healed:
            through = self._emitted_through.get(partition)
            if through is not None and event.timestamp <= through:
                self._unordered_drops += 1
                continue
            admitted.append(event)
        if admitted:
            newest = admitted[-1].timestamp
            current = self._emitted_through.get(partition)
            if current is None or newest > current:
                self._emitted_through[partition] = newest
        self._healed_windows[partition] = (
            *self._healed_windows.get(partition, ()),
            (gap.start, gap.end),
        )
        return tuple(admitted)


def _validate_file(file: FileQueryFile, request: DatasetRequest) -> None:
    if (
        file.asset_class is not request.asset_class
        or file.data_type is not request.data_type
        or file.symbol.strip().upper() != request.symbol
    ):
        raise MarketLakeError(
            "file query returned a file outside the requested dataset"
        )
    if file.object_path != partition_path(
        session_date=file.session_date,
        asset_class=LakeContractAssetClass(file.asset_class.value),
        data_type=LakeContractDataType(file.data_type.value),
        symbol=file.symbol,
    ):
        raise MarketLakeError("file query returned a non-canonical lake partition path")
    if not file.read_url.strip():
        raise MarketLakeError("file query returned an empty API-issued read URL")
    if file.byte_size < 0 or file.row_count < 0:
        raise MarketLakeError("file query returned negative file metadata")


def _overlap_dates(calendar: CoverageCalendar, gap: CoverageGap) -> tuple[date, ...]:
    return tuple(
        session.session_date
        for session in calendar.sessions(gap.start.date(), gap.end.date())
        if session.open_time < gap.end and session.close_time > gap.start
    )


def _healed_detail(admitted: int, fetched: int) -> str:
    if admitted == fetched:
        return f"replayed {fetched} lake events"
    return f"replayed {admitted} of {fetched} lake events"


def _reason_text(error: Exception) -> str:
    message = str(error)
    return message if message else type(error).__name__


def _partition(symbol: str, data_type: DataType | str) -> CoveragePartition:
    return (str(canonical_symbol(text(symbol, "symbol"))), DataType(data_type))


def _partition_key(partition: CoveragePartition) -> tuple[str, str]:
    return (partition[0], partition[1].value)


def _event_partition(event: MarketEvent) -> CoveragePartition:
    if isinstance(event, QuoteEvent):
        return (str(event.symbol), DataType.QUOTE)
    if isinstance(event, TradeEvent):
        return (str(event.symbol), DataType.TRADE)
    raise TypeError("event must be a QuoteEvent or TradeEvent")


def _positive_duration(value: timedelta, field_name: str) -> None:
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be a timedelta")
    if value <= timedelta(0):
        raise ValueError(f"{field_name} must be positive")


__all__ = [
    "CoverageCalendar",
    "CoverageGap",
    "HealingAction",
    "HealingStatus",
    "LakeGapBackfiller",
    "LakeSessionCalendar",
    "LiveGapDetector",
    "LiveGapHealer",
    "MarketEvent",
    "UncoveredGapError",
]
