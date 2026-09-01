from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from lake.session_ranges import crypto_utc_days, xnys_sessions


def test_xnys_sessions_exclude_holiday_and_weekend() -> None:
    sessions = xnys_sessions(date(2024, 11, 27), date(2024, 12, 2))

    assert [session.session_date for session in sessions] == [
        date(2024, 11, 27),
        date(2024, 11, 29),
        date(2024, 12, 2),
    ]


def test_xnys_sessions_preserve_half_day_close() -> None:
    sessions = xnys_sessions(date(2024, 11, 27), date(2024, 11, 29))

    assert sessions[0].open_time == datetime(2024, 11, 27, 14, 30, tzinfo=UTC)
    assert sessions[0].close_time == datetime(2024, 11, 27, 21, tzinfo=UTC)
    assert sessions[1].open_time == datetime(2024, 11, 29, 14, 30, tzinfo=UTC)
    assert sessions[1].close_time == datetime(2024, 11, 29, 18, tzinfo=UTC)


def test_crypto_utc_days_include_every_date() -> None:
    assert crypto_utc_days(date(2024, 11, 28), date(2024, 12, 2)) == (
        date(2024, 11, 28),
        date(2024, 11, 29),
        date(2024, 11, 30),
        date(2024, 12, 1),
        date(2024, 12, 2),
    )


def test_ranges_are_inclusive_of_a_single_date() -> None:
    day = date(2024, 12, 2)

    assert [session.session_date for session in xnys_sessions(day, day)] == [day]
    assert crypto_utc_days(day, day) == (day,)


@pytest.mark.parametrize("range_function", [xnys_sessions, crypto_utc_days])
def test_ranges_reject_reversed_dates(
    range_function: Callable[[date, date], object],
) -> None:
    with pytest.raises(ValueError, match="start must be on or before end"):
        range_function(date(2024, 12, 2), date(2024, 12, 1))
