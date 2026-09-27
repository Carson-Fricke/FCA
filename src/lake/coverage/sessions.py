"""Session helpers retained for Python consumers.

The coverage-planning machinery (catalog classification and gap planning) is
native Rust; this module keeps only the calendar helpers the soak driver and
the experiment scripts import: inclusive XNYS and UTC-day sessions with the
current-session flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from lake.contracts import AssetClass
from lake.contracts.sessions import crypto_utc_days, xnys_sessions


@dataclass(frozen=True, slots=True)
class CoverageSession:
    """A UTC session window, including whether it is currently open."""

    session_date: date
    open_time: datetime
    close_time: datetime
    is_partial: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.session_date, datetime):
            raise TypeError("session_date must be a date, not a datetime")
        if not isinstance(self.session_date, date):
            raise TypeError("session_date must be a date")
        if self.open_time.tzinfo is None or self.open_time.utcoffset() is None:
            raise ValueError("open_time must be timezone-aware")
        if self.close_time.tzinfo is None or self.close_time.utcoffset() is None:
            raise ValueError("close_time must be timezone-aware")
        open_time = self.open_time.astimezone(UTC)
        close_time = self.close_time.astimezone(UTC)
        if open_time >= close_time:
            raise ValueError("open_time must be before close_time")
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)


def sessions_for(
    asset_class: AssetClass,
    start: date,
    end: date,
    *,
    as_of: datetime | None = None,
) -> tuple[CoverageSession, ...]:
    """Return inclusive equity or crypto sessions with current-session flags."""
    asset_class = AssetClass(asset_class)
    _validate_date_range(start, end)
    current_time = _normalize_as_of(as_of)

    if asset_class is AssetClass.EQUITY:
        ranges = (
            CoverageSession(
                session_date=session.session_date,
                open_time=session.open_time,
                close_time=session.close_time,
                is_partial=session.open_time <= current_time < session.close_time,
            )
            for session in xnys_sessions(start, end)
        )
    elif asset_class is AssetClass.CRYPTO:
        ranges = (
            _crypto_session(session_date, current_time)
            for session_date in crypto_utc_days(start, end)
        )
    else:
        raise ValueError("coverage planning supports equity and crypto only")
    return tuple(ranges)


def equity_sessions(
    start: date, end: date, *, as_of: datetime | None = None
) -> tuple[CoverageSession, ...]:
    """Return XNYS coverage sessions for an inclusive range."""
    return sessions_for(AssetClass.EQUITY, start, end, as_of=as_of)


def crypto_sessions(
    start: date, end: date, *, as_of: datetime | None = None
) -> tuple[CoverageSession, ...]:
    """Return UTC-day coverage sessions for an inclusive range."""
    return sessions_for(AssetClass.CRYPTO, start, end, as_of=as_of)


def _crypto_session(session_date: date, as_of: datetime) -> CoverageSession:
    open_time = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    close_time = open_time + timedelta(days=1)
    return CoverageSession(
        session_date=session_date,
        open_time=open_time,
        close_time=close_time,
        is_partial=open_time <= as_of < close_time,
    )


def _normalize_as_of(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _validate_date_range(start: date, end: date) -> None:
    if isinstance(start, datetime) or isinstance(end, datetime):
        raise TypeError("start and end must be dates, not datetimes")
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError("start and end must be dates")
    if start > end:
        raise ValueError("start must be on or before end")
