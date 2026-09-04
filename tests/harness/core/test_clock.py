from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from harness.core.clock import (
    AdvanceableClock,
    Clock,
    ReadonlyClock,
    SimClock,
    WallClock,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def test_sim_clock_is_explicit_monotonic_and_utc_normalized() -> None:
    clock = SimClock(STAMP.astimezone(timezone(timedelta(hours=-5))))

    assert isinstance(clock, Clock)
    assert isinstance(clock, AdvanceableClock)
    assert clock.now() == STAMP
    assert clock.advance_by(timedelta(microseconds=1)) == STAMP + timedelta(
        microseconds=1
    )
    assert clock.head == STAMP + timedelta(microseconds=1)

    with pytest.raises(ValueError, match="older than the SimClock head"):
        clock.advance_to(STAMP)


def test_sim_clock_rejects_implicit_or_invalid_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(datetime(2024, 1, 19, 14, 30))

    clock = SimClock(STAMP)
    with pytest.raises(ValueError, match="must not be negative"):
        clock.advance_by(timedelta(microseconds=-1))


def test_wall_clock_is_an_explicit_utc_adapter() -> None:
    clock = WallClock()

    assert isinstance(clock, Clock)
    assert not isinstance(clock, AdvanceableClock)
    assert clock.now().tzinfo is UTC


def test_readonly_clock_delegates_observed_time_and_satisfies_clock_protocol() -> None:
    clock = SimClock(STAMP)
    view = ReadonlyClock(clock)

    assert isinstance(view, Clock)
    assert not isinstance(view, AdvanceableClock)
    assert view.now() == STAMP
    assert clock.advance_by(timedelta(microseconds=1)) == STAMP + timedelta(
        microseconds=1
    )
    assert view.now() == clock.now()


def test_readonly_clock_wraps_any_clock_implementation() -> None:
    wall = WallClock()
    view = ReadonlyClock(wall)
    nested = ReadonlyClock(ReadonlyClock(SimClock(STAMP)))

    assert view.now().tzinfo is UTC
    assert nested.now() == STAMP


def test_readonly_clock_exposes_no_mutation_surface() -> None:
    view = ReadonlyClock(SimClock(STAMP))

    assert not hasattr(view, "advance_to")
    assert not hasattr(view, "advance_by")
    assert not hasattr(view, "head")


def test_readonly_clock_rejects_mutation_of_itself() -> None:
    view = ReadonlyClock(SimClock(STAMP))

    with pytest.raises(AttributeError, match="read-only"):
        setattr(view, "_clock", WallClock())  # noqa: B010 - deliberate dynamic probe
    with pytest.raises(AttributeError, match="read-only"):
        setattr(view, "now", lambda: STAMP)  # noqa: B010 - deliberate dynamic probe
    with pytest.raises(AttributeError, match="read-only"):
        delattr(view, "_clock")


def test_readonly_clock_rejects_objects_without_now() -> None:
    with pytest.raises(TypeError, match="clock must implement Clock"):
        ReadonlyClock(cast(Clock, object()))
