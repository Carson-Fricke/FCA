//! Exchange and UTC-day session calendars.
//!
//! The equity calendar reproduces NYSE (XNYS) sessions: weekends, the fixed
//! and floating holidays, weekend observance, ad-hoc closures, and early
//! closes. The crypto calendar is every UTC calendar day.

use crate::error::DataError;
use crate::partitions::AssetClass;
use crate::time::{Date, DateTime};

/// An exchange session with its exact UTC trading bounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExchangeSession {
    /// UTC calendar date of the session.
    pub session_date: Date,
    /// Inclusive UTC open.
    pub open_time: DateTime,
    /// Exclusive UTC close.
    pub close_time: DateTime,
}

/// Whether `as_of` falls inside the session's trading window.
///
/// Mirrors the Python session clock: a session is partial while
/// `open_time <= as_of < close_time`.
pub fn is_partial(session: &ExchangeSession, as_of: DateTime) -> bool {
    session.open_time <= as_of && as_of < session.close_time
}

/// Every UTC calendar day in the inclusive date range.
pub fn crypto_utc_days(start: Date, end: Date) -> Result<Vec<Date>, DataError> {
    if start > end {
        return Err(DataError::value("start must be on or before end"));
    }
    Ok((0..=start.days_until(end))
        .map(|offset| start.add_days(offset))
        .collect())
}

/// Every UTC day session in the inclusive date range.
pub fn crypto_sessions(start: Date, end: Date) -> Result<Vec<ExchangeSession>, DataError> {
    Ok(crypto_utc_days(start, end)?
        .into_iter()
        .map(|session_date| ExchangeSession {
            session_date,
            open_time: DateTime::midnight(session_date),
            close_time: DateTime::midnight(session_date.add_days(1)),
        })
        .collect())
}

/// Sessions for one asset class: XNYS for equities, UTC days for crypto.
pub fn sessions_for(
    asset_class: AssetClass,
    start: Date,
    end: Date,
) -> Result<Vec<ExchangeSession>, DataError> {
    match asset_class {
        AssetClass::Equity => xnys_sessions(start, end),
        AssetClass::Crypto => crypto_sessions(start, end),
        other => Err(DataError::value(format!(
            "coverage planning supports equity and crypto only, not {other}"
        ))),
    }
}

/// XNYS sessions in the inclusive date range.
pub fn xnys_sessions(start: Date, end: Date) -> Result<Vec<ExchangeSession>, DataError> {
    if start > end {
        return Err(DataError::value("start must be on or before end"));
    }
    let mut sessions = Vec::new();
    let mut day = start;
    while day <= end {
        if is_session(day) {
            let close_hour = if is_early_close(day) { 13 } else { 16 };
            let open_local = day_seconds(day) + 9 * 3600 + 1800;
            let close_local = day_seconds(day) + close_hour * 3600;
            sessions.push(ExchangeSession {
                session_date: day,
                open_time: to_utc(day, open_local),
                close_time: to_utc(day, close_local),
            });
        }
        day = day.add_days(1);
    }
    Ok(sessions)
}

fn day_seconds(day: Date) -> i64 {
    day.days() as i64 * 86_400
}

/// Convert local wall-clock seconds since the epoch to UTC seconds applying
/// the US Eastern DST rule.
fn to_utc(day: Date, local_seconds: i64) -> DateTime {
    let offset = if is_dst(day) { 4 * 3600 } else { 5 * 3600 };
    DateTime::from_micros((local_seconds + offset) * 1_000_000)
}

fn is_dst(day: Date) -> bool {
    let (year, _, _) = day.ymd();
    let start = dst_start(year);
    let end = dst_end(year);
    day >= start && day < end
}

fn dst_start(year: i32) -> Date {
    nth_weekday(year, 3, 6, 2)
}

fn dst_end(year: i32) -> Date {
    nth_weekday(year, 11, 6, 1)
}

/// The `n`-th `weekday` (0 = Monday) of a month.
fn nth_weekday(year: i32, month: u32, weekday: u32, n: u32) -> Date {
    let first = Date::from_ymd(year, month, 1).expect("valid month start");
    let offset = (weekday as i32 - first.weekday_index() as i32).rem_euclid(7) + (n as i32 - 1) * 7;
    first.add_days(offset)
}

/// The last `weekday` (0 = Monday) of a month.
fn last_weekday(year: i32, month: u32, weekday: u32) -> Date {
    let first_next = if month == 12 {
        Date::from_ymd(year + 1, 1, 1).expect("valid")
    } else {
        Date::from_ymd(year, month + 1, 1).expect("valid")
    };
    let last = first_next.add_days(-1);
    let delta = (last.weekday_index() as i32 - weekday as i32).rem_euclid(7);
    last.add_days(-delta)
}

fn is_session(day: Date) -> bool {
    if day.weekday_index() > 4 {
        return false;
    }
    if is_adhoc_closure(day) {
        return false;
    }
    let (year, month, day_of_month) = day.ymd();
    !nyse_holidays(year)
        .iter()
        .any(|(m, d)| *m == month && *d == day_of_month)
}

fn is_early_close(day: Date) -> bool {
    let (year, month, day_of_month) = day.ymd();
    if !is_session(day) {
        return false;
    }
    // Independence Day eve (July 3) when Independence Day itself falls on a
    // weekday. When July 4 is Saturday, July 3 is the observed holiday.
    if month == 7 && day_of_month == 3 {
        return true;
    }
    // Christmas Eve when it is a trading day (Christmas falls on a weekday).
    if month == 12 && day_of_month == 24 {
        return true;
    }
    // Thanksgiving is the 4th Thursday of November; the following Friday.
    let thanksgiving = nth_weekday(year, 11, 3, 4);
    day == thanksgiving.add_days(1)
}

/// Ad-hoc full-day NYSE closures from 1994 onward.
fn is_adhoc_closure(day: Date) -> bool {
    let (year, month, day_of_month) = day.ymd();
    matches!(
        (year, month, day_of_month),
        (1994, 4, 27)          // Richard Nixon funeral
            | (2001, 9, 11)    // September 11 attacks
            | (2001, 9, 12)
            | (2001, 9, 13)
            | (2001, 9, 14)
            | (2004, 6, 11)    // Ronald Reagan funeral
            | (2007, 1, 2)     // Gerald Ford funeral
            | (2012, 10, 29)   // Hurricane Sandy
            | (2012, 10, 30)
            | (2018, 12, 5)    // George H. W. Bush funeral
            | (2025, 1, 9) // Jimmy Carter funeral
    )
}

fn observed(year: i32, month: u32, day: u32) -> (u32, u32) {
    let date = Date::from_ymd(year, month, day).expect("valid holiday");
    match date.weekday_index() {
        5 => {
            let shifted = date.add_days(-1);
            let (_, m, d) = shifted.ymd();
            (m, d)
        }
        6 => {
            let shifted = date.add_days(1);
            let (_, m, d) = shifted.ymd();
            (m, d)
        }
        _ => (month, day),
    }
}

fn nyse_holidays(year: i32) -> Vec<(u32, u32)> {
    let mut holidays = vec![
        observed(year, 1, 1),
        {
            let date = nth_weekday(year, 1, 0, 3);
            let (_, m, d) = date.ymd();
            (m, d)
        },
        {
            let date = nth_weekday(year, 2, 0, 3);
            let (_, m, d) = date.ymd();
            (m, d)
        },
        {
            let easter = easter(year);
            let good_friday = easter.add_days(-2);
            let (_, m, d) = good_friday.ymd();
            (m, d)
        },
        {
            let date = last_weekday(year, 5, 0);
            let (_, m, d) = date.ymd();
            (m, d)
        },
        observed(year, 7, 4),
        {
            let date = nth_weekday(year, 9, 0, 1);
            let (_, m, d) = date.ymd();
            (m, d)
        },
        {
            let date = nth_weekday(year, 11, 3, 4);
            let (_, m, d) = date.ymd();
            (m, d)
        },
        observed(year, 12, 25),
    ];
    if year >= 2022 {
        holidays.push(observed(year, 6, 19));
    }
    holidays
}

fn easter(year: i32) -> Date {
    // Anonymous Gregorian algorithm.
    let a = year % 19;
    let b = year / 100;
    let c = year % 100;
    let d = b / 4;
    let e = b % 4;
    let f = (b + 8) / 25;
    let g = (b - f + 1) / 3;
    let h = (19 * a + b - d - g + 15) % 30;
    let i = c / 4;
    let k = c % 4;
    let l = (32 + 2 * e + 2 * i - h - k) % 7;
    let m = (a + 11 * h + 22 * l) / 451;
    let month = (h + l - 7 * m + 114) / 31;
    let day = (h + l - 7 * m + 114) % 31 + 1;
    Date::from_ymd(year, month as u32, day as u32).expect("valid easter")
}
