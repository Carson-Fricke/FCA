"""Deterministic callbacks and XNYS session events over the event bus."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import ClassVar
from zoneinfo import ZoneInfo

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.types.events import Event, EventType
from lake.contracts import ExchangeSession, xnys_sessions

type Callback = Callable[..., None]


class SessionEventKind(StrEnum):
    """The deterministic lifecycle markers emitted for an XNYS session."""

    PRE_OPEN = "pre_open"
    OPEN = "open"
    HALF_DAY = "half_day"
    CLOSE = "close"


SessionEventType = SessionEventKind


@dataclass(frozen=True, slots=True)
class SessionEvent(Event):
    """One XNYS lifecycle marker published through ``EventBus``."""

    session_date: date
    kind: SessionEventKind
    open_time: datetime
    close_time: datetime
    is_half_day: bool
    event_type: ClassVar[EventType] = EventType.ORDER

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if isinstance(self.session_date, datetime) or not isinstance(
            self.session_date, date
        ):
            raise TypeError("session_date must be a date")
        object.__setattr__(self, "kind", SessionEventKind(self.kind))
        object.__setattr__(self, "open_time", _utc(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", _utc(self.close_time, "close_time"))
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if not isinstance(self.is_half_day, bool):
            raise TypeError("is_half_day must be a bool")

    @property
    def event(self) -> SessionEventKind:
        """Return the marker kind under a concise event-oriented name."""
        return self.kind

    @property
    def name(self) -> str:
        """Return the serialized marker name."""
        return self.kind.value


@dataclass(frozen=True, slots=True)
class ScheduledCallbackEvent(Event):
    """Internal/public event carrying one callback's explicit timestamp."""

    callback_id: int
    schedule_kind: str
    session_date: date | None = None
    session_kind: SessionEventKind | None = None
    event_type: ClassVar[EventType] = EventType.ORDER

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if (
            isinstance(self.callback_id, bool)
            or not isinstance(self.callback_id, int)
            or self.callback_id < 0
        ):
            raise ValueError("callback_id must be a non-negative integer")
        if not isinstance(self.schedule_kind, str) or not self.schedule_kind:
            raise ValueError("schedule_kind must be a non-empty string")
        if self.session_date is not None and (
            isinstance(self.session_date, datetime)
            or not isinstance(self.session_date, date)
        ):
            raise TypeError("session_date must be a date or None")
        if self.session_kind is not None:
            object.__setattr__(
                self, "session_kind", SessionEventKind(self.session_kind)
            )


@dataclass(frozen=True, slots=True)
class ScheduledHandle:
    """Stable identity returned by callback registration."""

    callback_id: int
    schedule_kind: str


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _date(value: date | datetime, field_name: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.date()
    if not isinstance(value, date):
        raise TypeError(f"{field_name} must be a date")
    return value


def _half_day(session: ExchangeSession) -> bool:
    return session.close_time - session.open_time < timedelta(hours=6, minutes=30)


class SimScheduler:
    """Single-threaded callback scheduler backed by ``SimClock`` and ``EventBus``.

    Callbacks are invoked synchronously by bus dispatch.  A callback accepting
    one positional argument receives its ``ScheduledCallbackEvent``; a callback
    accepting no positional arguments is also supported for simple strategies.
    """

    __slots__ = (
        "_bus",
        "_clock",
        "_callbacks",
        "_recurring",
        "_next_id",
        "_next_sequence",
        "_session_events",
    )

    def __init__(self, clock: SimClock, bus: EventBus) -> None:
        if not isinstance(clock, SimClock):
            raise TypeError("clock must be a SimClock")
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        self._clock = clock
        self._bus = bus
        self._callbacks: dict[int, Callback] = {}
        self._recurring: dict[int, _RecurringCallback] = {}
        self._next_id = 0
        self._next_sequence = 0
        self._session_events: set[tuple[date, SessionEventKind]] = set()
        self._bus.subscribe(ScheduledCallbackEvent, self._dispatch_callback)

    @property
    def clock(self) -> SimClock:
        """Return the injected simulation clock."""
        return self._clock

    @property
    def bus(self) -> EventBus:
        """Return the injected synchronous event bus."""
        return self._bus

    def schedule_at(self, when: datetime, callback: Callback) -> ScheduledHandle:
        """Schedule one callback at an absolute, timezone-aware timestamp."""
        event_time = _utc(when, "when")
        self._ensure_future(event_time)
        callback_id = self._allocate(callback)
        self._schedule_event(event_time, callback_id, "absolute")
        return ScheduledHandle(callback_id, "absolute")

    def schedule(
        self, when: datetime | Event, callback: Callback | None = None
    ) -> ScheduledHandle | None:
        """Schedule a callback or an already timestamped bus event.

        The event form keeps this scheduler compatible with the strategy
        scheduler protocol, while the timestamp form registers a callback.
        """
        if isinstance(when, Event):
            if callback is not None:
                raise TypeError("callback must be omitted when scheduling an Event")
            self._bus.schedule(when)
            return None
        if callback is None:
            raise TypeError("callback is required for a timestamp schedule")
        return self.schedule_at(when, callback)

    def schedule_every(
        self,
        interval: timedelta,
        callback: Callback,
        *,
        start: datetime | None = None,
        count: int | None = None,
        end: datetime | None = None,
    ) -> ScheduledHandle:
        """Schedule a fixed-duration recurring callback.

        The first call is at ``start`` when supplied, otherwise one interval
        after the current simulation head.  Recurrence is based on the prior
        scheduled timestamp, not on callback runtime.
        """
        if not isinstance(interval, timedelta) or interval <= timedelta(0):
            raise ValueError("interval must be a positive timedelta")
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count <= 0
        ):
            raise ValueError("count must be a positive integer or None")
        end_time = None if end is None else _utc(end, "end")
        first = self._clock.now() + interval if start is None else _utc(start, "start")
        if end_time is not None and first > end_time:
            raise ValueError("start must not be after end")
        self._ensure_future(first)
        callback_id = self._allocate(callback)
        self._schedule_event(
            first,
            callback_id,
            "recurring",
        )
        self._recurring[callback_id] = _RecurringCallback(interval, count, end_time)
        return ScheduledHandle(callback_id, "recurring")

    def recurring(
        self,
        interval: timedelta,
        callback: Callback,
        *,
        start: datetime | None = None,
        count: int | None = None,
        end: datetime | None = None,
    ) -> ScheduledHandle:
        """Alias for ``schedule_every``."""
        return self.schedule_every(
            interval, callback, start=start, count=count, end=end
        )

    def schedule_session_events(
        self,
        start: date | datetime,
        end: date | datetime | None = None,
        *,
        pre_open: timedelta = timedelta(minutes=15),
    ) -> tuple[SessionEvent, ...]:
        """Publish each requested XNYS session marker exactly once.

        Normal sessions receive pre-open, open, and close markers.  Early-close
        sessions additionally receive one half-day marker immediately before
        the close marker.  Calendar timestamps are converted from the exchange
        contract to UTC, so DST transitions do not change local session intent.
        """
        start_date = _date(start, "start")
        end_date = start_date if end is None else _date(end, "end")
        if end_date < start_date:
            raise ValueError("end must not be before start")
        if not isinstance(pre_open, timedelta) or pre_open < timedelta(0):
            raise ValueError("pre_open must be a non-negative timedelta")
        planned: list[tuple[ExchangeSession, SessionEventKind, datetime, bool]] = []
        for session in xnys_sessions(start_date, end_date):
            is_half_day = _half_day(session)
            markers = [
                (SessionEventKind.PRE_OPEN, session.open_time - pre_open),
                (SessionEventKind.OPEN, session.open_time),
            ]
            if is_half_day:
                markers.append((SessionEventKind.HALF_DAY, session.close_time))
            markers.append((SessionEventKind.CLOSE, session.close_time))
            planned.extend(
                (session, kind, event_time, is_half_day)
                for kind, event_time in markers
                if (session.session_date, kind) not in self._session_events
            )
        for _, _, event_time, _ in planned:
            self._ensure_future(event_time)
        result: list[SessionEvent] = []
        for session, kind, event_time, is_half_day in planned:
            key = (session.session_date, kind)
            event = SessionEvent(
                event_id=f"session-{session.session_date.isoformat()}-{kind.value}",
                timestamp=event_time,
                sequence=self._sequence(),
                session_date=session.session_date,
                kind=kind,
                open_time=session.open_time,
                close_time=session.close_time,
                is_half_day=is_half_day,
            )
            self._bus.schedule(event)
            self._session_events.add(key)
            result.append(event)
        return tuple(result)

    def schedule_session_relative(
        self,
        callback: Callback,
        *,
        start: date | datetime,
        end: date | datetime | None = None,
        offset: timedelta = timedelta(0),
        anchor: SessionEventKind | str = SessionEventKind.OPEN,
        at: time | None = None,
        include_half_days: bool = True,
    ) -> tuple[ScheduledHandle, ...]:
        """Schedule one callback per XNYS session at a local or relative time.

        ``at`` is interpreted as New York wall time on each session date.  If
        omitted, ``offset`` is applied to the selected ``anchor`` (open, close,
        pre-open, or half-day marker).  The timestamp placed on the bus is UTC.
        """
        _validate_callback(callback)
        if not isinstance(offset, timedelta):
            raise TypeError("offset must be a timedelta")
        selected_anchor = SessionEventKind(anchor)
        sessions = self._sessions(start, end)
        planned: list[tuple[ExchangeSession, datetime]] = []
        for session in sessions:
            is_half_day = _half_day(session)
            if not include_half_days and is_half_day:
                continue
            if selected_anchor is SessionEventKind.HALF_DAY and not is_half_day:
                continue
            if at is None:
                anchor_time = self._anchor_time(session, selected_anchor)
                callback_time = anchor_time + offset
            else:
                if not isinstance(at, time):
                    raise TypeError("at must be a time or None")
                if at.tzinfo is not None:
                    raise ValueError("at must be a naive New York wall time")
                local = datetime.combine(session.session_date, at)
                callback_time = local.replace(tzinfo=_new_york()).astimezone(UTC)
            self._ensure_future(callback_time)
            planned.append((session, callback_time))
        handles: list[ScheduledHandle] = []
        for session, callback_time in planned:
            callback_id = self._allocate(callback)
            self._schedule_event(
                callback_time,
                callback_id,
                "session",
                session_date=session.session_date,
                session_kind=selected_anchor,
            )
            handles.append(ScheduledHandle(callback_id, "session"))
        return tuple(handles)

    def schedule_session(
        self,
        callback: Callback,
        *,
        start: date | datetime,
        end: date | datetime | None = None,
        offset: timedelta = timedelta(0),
        anchor: SessionEventKind | str = SessionEventKind.OPEN,
        at: time | None = None,
        include_half_days: bool = True,
    ) -> tuple[ScheduledHandle, ...]:
        """Alias for ``schedule_session_relative``."""
        return self.schedule_session_relative(
            callback,
            start=start,
            end=end,
            offset=offset,
            anchor=anchor,
            at=at,
            include_half_days=include_half_days,
        )

    def run(self, limit: int | None = None) -> tuple[Event, ...]:
        """Dispatch due and scheduled events synchronously in bus order."""
        return self._bus.drain(limit)

    def drain(self, limit: int | None = None) -> tuple[Event, ...]:
        """Alias for ``run``."""
        return self.run(limit)

    def _sessions(
        self, start: date | datetime, end: date | datetime | None
    ) -> tuple[ExchangeSession, ...]:
        start_date = _date(start, "start")
        end_date = start_date if end is None else _date(end, "end")
        if end_date < start_date:
            raise ValueError("end must not be before start")
        return xnys_sessions(start_date, end_date)

    @staticmethod
    def _anchor_time(session: ExchangeSession, anchor: SessionEventKind) -> datetime:
        if anchor is SessionEventKind.PRE_OPEN:
            return session.open_time - timedelta(minutes=15)
        if anchor is SessionEventKind.OPEN:
            return session.open_time
        if anchor in {SessionEventKind.CLOSE, SessionEventKind.HALF_DAY}:
            return session.close_time
        raise ValueError(f"unsupported session anchor: {anchor}")

    def _allocate(self, callback: Callback) -> int:
        _validate_callback(callback)
        callback_id = self._next_id
        self._next_id += 1
        self._callbacks[callback_id] = callback
        return callback_id

    def _sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    def _schedule_event(
        self,
        when: datetime,
        callback_id: int,
        schedule_kind: str,
        *,
        session_date: date | None = None,
        session_kind: SessionEventKind | None = None,
    ) -> None:
        event = ScheduledCallbackEvent(
            event_id=f"callback-{callback_id}-{self._sequence()}",
            timestamp=when,
            sequence=self._next_sequence - 1,
            callback_id=callback_id,
            schedule_kind=schedule_kind,
            session_date=session_date,
            session_kind=session_kind,
        )
        self._bus.schedule(event)

    def _dispatch_callback(self, event: ScheduledCallbackEvent) -> None:
        callback = self._callbacks.get(event.callback_id)
        if callback is None:
            return
        recurring = self._recurring.get(event.callback_id)
        if recurring is not None:
            callback_fn = callback
            next_time = event.timestamp + recurring.interval
            remaining = None if recurring.remaining is None else recurring.remaining - 1
            if (remaining is None or remaining > 0) and (
                recurring.end is None or next_time <= recurring.end
            ):
                self._schedule_event(
                    next_time,
                    event.callback_id,
                    "recurring",
                )
                self._recurring[event.callback_id] = _RecurringCallback(
                    recurring.interval, remaining, recurring.end
                )
            else:
                self._callbacks.pop(event.callback_id, None)
                self._recurring.pop(event.callback_id, None)
        else:
            callback_fn = callback
            self._callbacks.pop(event.callback_id, None)
        _invoke(callback_fn, event)

    def _ensure_future(self, value: datetime) -> None:
        if value < self._clock.now():
            raise ValueError("scheduled callback cannot precede clock head")


@dataclass(frozen=True, slots=True)
class _RecurringCallback:
    interval: timedelta
    remaining: int | None
    end: datetime | None


def _new_york() -> ZoneInfo:
    return ZoneInfo("America/New_York")


def _validate_callback(callback: object) -> None:
    if not callable(callback):
        raise TypeError("callback must be callable")


def _invoke(callback: Callback, event: ScheduledCallbackEvent) -> None:
    parameters = tuple(inspect.signature(callback).parameters.values())
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    accepts_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )
    if positional or accepts_varargs:
        callback(event)
    else:
        callback()


Scheduler = SimScheduler
DeterministicScheduler = SimScheduler


__all__ = [
    "Callback",
    "DeterministicScheduler",
    "ScheduledCallbackEvent",
    "ScheduledHandle",
    "Scheduler",
    "SessionEvent",
    "SessionEventKind",
    "SessionEventType",
    "SimScheduler",
]
