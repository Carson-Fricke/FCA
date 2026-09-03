from datetime import UTC, date, datetime, time, timedelta

from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.scheduler import (
    Scheduler,
    SessionEvent,
    SessionEventKind,
)


def test_absolute_and_recurring_callbacks_are_synchronous_and_deterministic() -> None:
    start = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)
    clock = SimClock(start)
    bus = EventBus(clock)
    scheduler = Scheduler(clock, bus)
    seen: list[tuple[str, datetime]] = []

    scheduler.schedule_at(
        start + timedelta(seconds=2), lambda event: seen.append(("a", event.timestamp))
    )
    scheduler.schedule_every(
        timedelta(seconds=1),
        lambda event: seen.append(("r", event.timestamp)),
        start=start + timedelta(seconds=1),
        count=3,
    )

    scheduler.run()

    assert seen == [
        ("r", start + timedelta(seconds=1)),
        ("a", start + timedelta(seconds=2)),
        ("r", start + timedelta(seconds=2)),
        ("r", start + timedelta(seconds=3)),
    ]
    assert clock.now() == start + timedelta(seconds=3)


def test_xnys_session_events_are_once_per_marker_for_normal_half_day_and_dst() -> None:
    clock = SimClock(datetime(2024, 3, 8, 12, tzinfo=UTC))
    bus = EventBus(clock)
    scheduler = Scheduler(clock, bus)
    received: list[SessionEvent] = []
    bus.subscribe(SessionEvent, received.append)

    scheduler.schedule_session_events(date(2024, 3, 8), date(2024, 3, 11))
    scheduler.schedule_session_events(date(2024, 3, 8), date(2024, 3, 11))
    scheduler.run()

    assert [(event.session_date, event.kind) for event in received] == [
        (date(2024, 3, 8), SessionEventKind.PRE_OPEN),
        (date(2024, 3, 8), SessionEventKind.OPEN),
        (date(2024, 3, 8), SessionEventKind.CLOSE),
        (date(2024, 3, 11), SessionEventKind.PRE_OPEN),
        (date(2024, 3, 11), SessionEventKind.OPEN),
        (date(2024, 3, 11), SessionEventKind.CLOSE),
    ]
    assert received[1].timestamp == datetime(2024, 3, 8, 14, 30, tzinfo=UTC)
    assert received[4].timestamp == datetime(2024, 3, 11, 13, 30, tzinfo=UTC)


def test_session_relative_new_york_time_tracks_dst_and_marks_half_day() -> None:
    clock = SimClock(datetime(2024, 11, 27, 12, tzinfo=UTC))
    bus = EventBus(clock)
    scheduler = Scheduler(clock, bus)
    callback_times: list[datetime] = []
    session_events: list[SessionEvent] = []
    bus.subscribe(SessionEvent, session_events.append)

    scheduler.schedule_session_events(date(2024, 11, 27), date(2024, 11, 29))
    scheduler.schedule_session_relative(
        lambda event: callback_times.append(event.timestamp),
        start=date(2024, 11, 27),
        end=date(2024, 11, 29),
        at=time(9, 35),
    )
    scheduler.run()

    assert callback_times == [
        datetime(2024, 11, 27, 14, 35, tzinfo=UTC),
        datetime(2024, 11, 29, 14, 35, tzinfo=UTC),
    ]
    half_day = [
        event for event in session_events if event.kind is SessionEventKind.HALF_DAY
    ]
    assert len(half_day) == 1
    assert half_day[0].session_date == date(2024, 11, 29)
    assert half_day[0].timestamp == datetime(2024, 11, 29, 18, tzinfo=UTC)
