"""Inclusive market session and UTC-day ranges."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import exchange_calendars

_CALENDAR_PADDING = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class ExchangeSession:
    """An exchange session with its exact UTC trading bounds."""

    session_date: date
    open_time: datetime
    close_time: datetime


def xnys_sessions(start: date, end: date) -> tuple[ExchangeSession, ...]:
    """Return XNYS sessions in the inclusive date range."""
    _validate_range(start, end)
    calendar = exchange_calendars.get_calendar(
        "XNYS",
        start=start - _CALENDAR_PADDING,
        end=end + _CALENDAR_PADDING,
    )

    return tuple(
        ExchangeSession(
            session_date=session.date(),
            open_time=calendar.session_open(session).to_pydatetime().astimezone(UTC),
            close_time=calendar.session_close(session).to_pydatetime().astimezone(UTC),
        )
        for session in calendar.sessions_in_range(start, end)
    )


def crypto_utc_days(start: date, end: date) -> tuple[date, ...]:
    """Return every UTC calendar day in the inclusive date range."""
    _validate_range(start, end)
    return tuple(
        start + timedelta(days=offset) for offset in range((end - start).days + 1)
    )


def _validate_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("start must be on or before end")
