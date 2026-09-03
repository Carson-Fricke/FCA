from datetime import UTC, datetime, timedelta, timezone

import pytest

from harness.core.clock import AdvanceableClock, Clock, SimClock, WallClock

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
