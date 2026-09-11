//! Minimal, dependency-free UTC calendar primitives.
//!
//! Python's data layer exchanges timezone-aware `datetime` and `date` values.
//! The Rust port keeps those at microsecond resolution ([`DateTime`]) and
//! exposes the integer nanosecond timeline ([`Nanos`]) used by the history
//! boundary. The civil-calendar conversions are Howard Hinnant's well-known
//! `days_from_civil`/`civil_from_days` algorithms, which round-trip proleptic
//! Gregorian dates exactly.

use core::fmt;

/// Integer nanoseconds since the Unix epoch.
pub type Nanos = i64;

const MICROS_PER_SECOND: i64 = 1_000_000;
const MICROS_PER_DAY: i64 = 86_400 * MICROS_PER_SECOND;
const NANOS_PER_MICRO: i64 = 1_000;

/// A proleptic Gregorian calendar date, stored as days since 1970-01-01.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Date {
    days: i32,
}

impl Date {
    /// 1970-01-01.
    pub const EPOCH: Date = Date { days: 0 };

    /// Build a date from a day offset relative to the Unix epoch.
    pub const fn from_days(days: i32) -> Self {
        Self { days }
    }

    /// The day offset relative to the Unix epoch.
    pub const fn days(self) -> i32 {
        self.days
    }

    /// Build a date from calendar parts, rejecting impossible values.
    pub fn from_ymd(year: i32, month: u32, day: u32) -> Option<Self> {
        if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
            return None;
        }
        let days = days_from_civil(year, month, day);
        let date = Self::from_days(days);
        let (y, m, d) = date.ymd();
        if (y, m, d) != (year, month, day) {
            return None;
        }
        Some(date)
    }

    /// The calendar parts `(year, month, day)`.
    pub fn ymd(self) -> (i32, u32, u32) {
        civil_from_days(self.days)
    }

    /// The `YYYY-MM-DD` ISO-8601 rendering used by partition paths.
    pub fn isoformat(self) -> String {
        let (year, month, day) = self.ymd();
        format!("{year:04}-{month:02}-{day:02}")
    }

    /// A new date offset by `offset` days.
    pub fn add_days(self, offset: i32) -> Self {
        Self::from_days(self.days + offset)
    }

    /// The number of days from `self` to `other`.
    pub fn days_until(self, other: Date) -> i32 {
        other.days - self.days
    }
}

impl fmt::Display for Date {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.isoformat())
    }
}

/// A UTC instant at microsecond resolution.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct DateTime {
    micros: i64,
}

impl DateTime {
    /// The Unix epoch instant.
    pub const EPOCH: DateTime = DateTime { micros: 0 };

    /// Build from microseconds since the Unix epoch.
    pub const fn from_micros(micros: i64) -> Self {
        Self { micros }
    }

    /// Microseconds since the Unix epoch.
    pub const fn micros(self) -> i64 {
        self.micros
    }

    /// Build the UTC instant from an integer nanosecond timeline.
    ///
    /// Mirrors Python's `from_nanos`, which floors to microseconds. Negative
    /// instants floor toward negative infinity (`div_euclid`).
    pub fn from_nanos(nanos: Nanos) -> Self {
        Self {
            micros: nanos.div_euclid(NANOS_PER_MICRO),
        }
    }

    /// Convert to the integer nanosecond timeline.
    ///
    /// Mirrors Python's `to_nanos` for a microsecond-resolution `datetime`.
    pub fn to_nanos(self) -> Nanos {
        self.micros * NANOS_PER_MICRO
    }

    /// Build from calendar parts at second and microsecond precision.
    pub fn from_ymd_hms(
        year: i32,
        month: u32,
        day: u32,
        hour: u32,
        minute: u32,
        second: u32,
        micros: u32,
    ) -> Option<Self> {
        let date = Date::from_ymd(year, month, day)?;
        if hour > 23 || minute > 59 || second > 59 || micros >= 1_000_000 {
            return None;
        }
        let seconds = hour as i64 * 3_600 + minute as i64 * 60 + second as i64;
        let day_micros = date.days() as i64 * MICROS_PER_DAY;
        Some(Self::from_micros(
            day_micros + seconds * MICROS_PER_SECOND + micros as i64,
        ))
    }

    /// Midnight UTC on `date`.
    pub fn midnight(date: Date) -> Self {
        Self::from_micros(date.days() as i64 * MICROS_PER_DAY)
    }

    /// The UTC calendar date containing this instant.
    pub fn date(self) -> Date {
        Date::from_days(self.micros.div_euclid(MICROS_PER_DAY) as i32)
    }

    /// The `YYYY-MM-DDTHH:MM:SS.ffffff` ISO rendering at microsecond precision.
    pub fn isoformat_micros(self) -> String {
        let micros = self.micros;
        let day_micros = micros.div_euclid(MICROS_PER_DAY);
        let mut remainder = micros.rem_euclid(MICROS_PER_DAY);
        let day = Date::from_days(day_micros as i32);
        let seconds = remainder / MICROS_PER_SECOND;
        remainder %= MICROS_PER_SECOND;
        let (year, month, date) = day.ymd();
        let hour = seconds / 3_600;
        let minute = (seconds % 3_600) / 60;
        let second = seconds % 60;
        format!("{year:04}-{month:02}-{date:02}T{hour:02}:{minute:02}:{second:02}.{remainder:06}")
    }

    /// The UTC ISO rendering Python's `datetime.isoformat(timespec="microseconds")`
    /// produces for a timezone-aware UTC instant: a `+00:00` offset suffix.
    pub fn isoformat_micros_utc(self) -> String {
        format!("{}+00:00", self.isoformat_micros())
    }

    /// Parse an ISO-8601 UTC timestamp as Alpaca and lake files emit it.
    ///
    /// Accepts `Z`, `+00:00`, and explicit offsets; fractional seconds beyond
    /// six digits are truncated to microseconds like Python's `fromisoformat`.
    pub fn parse_iso_utc(text: &str) -> Option<Self> {
        let text = text.trim();
        let (body, offset_seconds) = if let Some(stripped) = text.strip_suffix('Z') {
            (stripped, 0i64)
        } else if let Some(index) = text.rfind(['+', '-']) {
            if index <= 10 {
                return None;
            }
            let (body, offset) = text.split_at(index);
            let sign = if offset.starts_with('+') { 1 } else { -1 };
            let digits: String = offset[1..].chars().filter(|c| c.is_ascii_digit()).collect();
            if digits.len() != 4 {
                return None;
            }
            let hours: i64 = digits[..2].parse().ok()?;
            let minutes: i64 = digits[2..].parse().ok()?;
            (body, sign * (hours * 3_600 + minutes * 60))
        } else {
            (text, 0)
        };
        let (date_part, time_part) = body.split_once('T')?;
        let mut date_parts = date_part.split('-');
        let year: i32 = date_parts.next()?.parse().ok()?;
        let month: u32 = date_parts.next()?.parse().ok()?;
        let day: u32 = date_parts.next()?.parse().ok()?;
        let date = Date::from_ymd(year, month, day)?;
        let (clock, fraction) = match time_part.split_once('.') {
            Some((clock, fraction)) => (clock, fraction),
            None => (time_part, ""),
        };
        let mut clock_parts = clock.split(':');
        let hour: u32 = clock_parts.next()?.parse().ok()?;
        let minute: u32 = clock_parts.next()?.parse().ok()?;
        let second: u32 = clock_parts.next()?.parse().ok()?;
        let mut micros = 0u32;
        let mut scale = 100_000u32;
        for digit in fraction.chars().filter(|c| c.is_ascii_digit()).take(6) {
            micros += digit.to_digit(10)? * scale;
            scale /= 10;
        }
        let seconds = hour as i64 * 3_600 + minute as i64 * 60 + second as i64;
        let local =
            date.days() as i64 * MICROS_PER_DAY + seconds * MICROS_PER_SECOND + micros as i64;
        Some(Self::from_micros(
            local - offset_seconds * MICROS_PER_SECOND,
        ))
    }
}

impl fmt::Display for DateTime {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.micros)
    }
}

/// Convert `(year, month, day)` to days since 1970-01-01.
fn days_from_civil(year: i32, month: u32, day: u32) -> i32 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let adjusted_month = if month > 2 {
        month as i32 - 3
    } else {
        month as i32 + 9
    };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day as i32 - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// Convert days since 1970-01-01 to `(year, month, day)`.
fn civil_from_days(days: i32) -> (i32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    (year + i32::from(month <= 2), month as u32, day as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_round_trip() {
        for (y, m, d) in [
            (1970, 1, 1),
            (2000, 2, 29),
            (1999, 12, 31),
            (2024, 7, 4),
            (1900, 1, 1),
            (2100, 12, 31),
        ] {
            let date = Date::from_ymd(y, m, d).unwrap();
            assert_eq!(date.ymd(), (y, m, d));
            assert_eq!(Date::from_days(date.days()), date);
        }
        assert_eq!(Date::EPOCH.isoformat(), "1970-01-01");
        assert_eq!(
            Date::from_ymd(2024, 1, 5).unwrap().isoformat(),
            "2024-01-05"
        );
        assert!(Date::from_ymd(2024, 2, 30).is_none());
    }

    #[test]
    fn nanos_floor_to_microseconds() {
        let instant = DateTime::from_micros(1_500);
        assert_eq!(instant.to_nanos(), 1_500_000);
        assert_eq!(DateTime::from_nanos(1_500_499).micros(), 1_500);
        assert_eq!(DateTime::from_nanos(-1).micros(), -1);
    }

    #[test]
    fn date_of_instant_is_utc() {
        let instant = DateTime::from_ymd_hms(2024, 1, 5, 13, 30, 0, 0).unwrap();
        assert_eq!(instant.date().isoformat(), "2024-01-05");
        assert_eq!(
            DateTime::midnight(Date::from_ymd(2024, 1, 5).unwrap()),
            DateTime::from_ymd_hms(2024, 1, 5, 0, 0, 0, 0).unwrap()
        );
    }
}
