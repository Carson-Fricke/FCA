//! Nasdaq Trader symbol directory snapshot parsing.
//!
//! Ports `lake.reference.nasdaq`. The daily `nasdaqtraded.txt` file is a
//! pipe-delimited directory whose final record carries the file creation
//! timestamp. That timestamp is the point-in-time knowledge key: each
//! snapshot is archived as its own partition dated by the creation
//! timestamp's US/Eastern calendar date.

use lake_contracts::{Date, DateTime, Decimal18};

use crate::error::ReferenceError;

const HEADER: [&str; 12] = [
    "Nasdaq Traded",
    "Symbol",
    "Security Name",
    "Listing Exchange",
    "Market Category",
    "ETF",
    "Round Lot Size",
    "Test Issue",
    "Financial Status",
    "CQS Symbol",
    "NASDAQ Symbol",
    "NextShares",
];

const FILE_CREATION_LABEL: &str = "File Creation Time";

const FIELD_COUNT: usize = HEADER.len();

/// One security row of a Nasdaq Trader symbol directory snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NasdaqSecurity {
    /// The trading symbol.
    pub symbol: String,
    /// The full security name.
    pub security_name: String,
    /// The listing exchange code.
    pub listing_exchange: String,
    /// The market category code.
    pub market_category: String,
    /// Whether the security is an exchange-traded fund.
    pub etf: bool,
    /// The round lot size at lake decimal scale.
    pub round_lot_size: Decimal18,
    /// Whether the security is a test issue.
    pub test_issue: bool,
    /// The financial status code, when present.
    pub financial_status: Option<String>,
}

/// A UTC instant carrying the US/Eastern wall clock it was parsed from.
///
/// The Parquet timestamp column stores the UTC instant; ingest summaries
/// render the Python-parity local wall clock with its offset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EasternInstant {
    utc: DateTime,
    local_date: Date,
    hour: u32,
    minute: u32,
    offset_seconds: i64,
}

impl EasternInstant {
    /// The UTC instant.
    pub fn utc(self) -> DateTime {
        self.utc
    }

    /// The `YYYY-MM-DDTHH:MM:SS±HH:MM` rendering of the local wall clock,
    /// exactly what Python's aware `datetime.isoformat()` produces.
    pub fn isoformat(self) -> String {
        let offset_minutes = self.offset_seconds / 60;
        let sign = if offset_minutes < 0 { '+' } else { '-' };
        let magnitude = offset_minutes.abs();
        format!(
            "{}T{:02}:{:02}:00{sign}{:02}:{:02}",
            self.local_date.isoformat(),
            self.hour,
            self.minute,
            magnitude / 60,
            magnitude % 60
        )
    }
}

/// One parsed symbol directory snapshot and its knowledge instant.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NasdaqSnapshot {
    /// The file creation instant in US/Eastern and UTC.
    pub file_creation_time: EasternInstant,
    /// The knowledge date: the creation timestamp's US/Eastern calendar date.
    pub snapshot_date: Date,
    /// The security rows, sorted by symbol.
    pub securities: Vec<NasdaqSecurity>,
}

/// Parse a nasdaqtraded.txt payload into securities plus creation instant.
pub fn parse_nasdaq_traded(payload: &[u8]) -> Result<NasdaqSnapshot, ReferenceError> {
    let text = String::from_utf8_lossy(payload);
    let text: &str = if let Some(stripped) = text.strip_prefix('\u{feff}') {
        stripped
    } else {
        &text
    };
    let lines: Vec<&str> = text
        .lines()
        .filter(|line| !line.trim().is_empty())
        .collect();
    if lines.len() < 3 {
        return Err(ReferenceError::nasdaq(
            "nasdaqtraded payload must contain a header, securities, and creation line",
        ));
    }
    let header: Vec<&str> = lines[0].split('|').map(str::trim).collect();
    if header.as_slice() != HEADER.as_slice() {
        return Err(ReferenceError::nasdaq(format!(
            "unexpected nasdaqtraded header: {:?}",
            lines[0]
        )));
    }

    let footer: Vec<&str> = lines[lines.len() - 1].split('|').collect();
    if footer.len() != 2 || footer[0].trim() != FILE_CREATION_LABEL {
        return Err(ReferenceError::nasdaq(format!(
            "missing file creation line: {:?}",
            lines[lines.len() - 1]
        )));
    }
    let creation_time = parse_file_creation_time(footer[1].trim())?;

    let mut securities: Vec<NasdaqSecurity> = Vec::new();
    for (index, line) in lines[1..lines.len() - 1].iter().enumerate() {
        let fields: Vec<&str> = line.split('|').collect();
        let line_number = index + 2;
        if fields.len() != FIELD_COUNT {
            return Err(ReferenceError::nasdaq(format!(
                "nasdaqtraded line {line_number} has {} of {FIELD_COUNT} fields",
                fields.len()
            )));
        }
        securities.push(parse_security(&fields, line_number)?);
    }

    securities.sort_by(|left, right| left.symbol.cmp(&right.symbol));
    Ok(NasdaqSnapshot {
        file_creation_time: creation_time,
        snapshot_date: creation_time.local_date,
        securities,
    })
}

fn parse_security(fields: &[&str], line_number: usize) -> Result<NasdaqSecurity, ReferenceError> {
    let symbol = fields[1].trim();
    if symbol.is_empty() {
        return Err(ReferenceError::nasdaq(format!(
            "nasdaqtraded line {line_number} has an empty symbol"
        )));
    }
    let security_name = fields[2].trim();
    if security_name.is_empty() {
        return Err(ReferenceError::nasdaq(format!(
            "nasdaqtraded line {line_number} has an empty security name"
        )));
    }
    let round_lot_text = fields[6].trim();
    let round_lot_size = Decimal18::parse(round_lot_text).map_err(|_| {
        ReferenceError::nasdaq(format!(
            "nasdaqtraded line {line_number} has an invalid round lot size: {round_lot_text:?}"
        ))
    })?;
    if round_lot_size <= Decimal18::ZERO {
        return Err(ReferenceError::nasdaq(format!(
            "nasdaqtraded line {line_number} round lot size must be positive: {round_lot_text:?}"
        )));
    }
    let status = fields[8].trim();
    Ok(NasdaqSecurity {
        symbol: symbol.to_string(),
        security_name: security_name.to_string(),
        listing_exchange: fields[3].trim().to_string(),
        market_category: fields[4].trim().to_string(),
        etf: parse_flag(fields[5], line_number)?,
        round_lot_size,
        test_issue: parse_flag(fields[7], line_number)?,
        financial_status: if status.is_empty() {
            None
        } else {
            Some(status.to_string())
        },
    })
}

fn parse_flag(value: &str, line_number: usize) -> Result<bool, ReferenceError> {
    match value.trim() {
        "Y" => Ok(true),
        "N" => Ok(false),
        other => Err(ReferenceError::nasdaq(format!(
            "nasdaqtraded line {line_number} flag must be Y or N: {other:?}"
        ))),
    }
}

/// Parse the `MMDDYYYYHH[:]MM##?` file creation footer into an Eastern
/// instant. Exactly one `#` is required and a second is tolerated, mirroring
/// the Python regular expression `(\d{2})(\d{2})(\d{4})(\d{2}):?(\d{2})##?`.
fn parse_file_creation_time(value: &str) -> Result<EasternInstant, ReferenceError> {
    let bytes = value.as_bytes();
    let mut digits = [0u32; 10];
    for (index, digit) in digits.iter_mut().enumerate() {
        let Some(byte) = bytes.get(index).filter(|byte| byte.is_ascii_digit()) else {
            return Err(invalid_creation_time(value));
        };
        *digit = u32::from(byte - b'0');
    }
    let month = digits[0] * 10 + digits[1];
    let day = digits[2] * 10 + digits[3];
    let year = digits[4] * 1000 + digits[5] * 100 + digits[6] * 10 + digits[7];
    let hour = digits[8] * 10 + digits[9];
    let mut index = 10;
    if bytes.get(index) == Some(&b':') {
        index += 1;
    }
    let mut minute_bytes = [0u32; 2];
    for (offset, digit) in minute_bytes.iter_mut().enumerate() {
        let Some(byte) = bytes
            .get(index + offset)
            .filter(|byte| byte.is_ascii_digit())
        else {
            return Err(invalid_creation_time(value));
        };
        *digit = u32::from(byte - b'0');
    }
    let minute = minute_bytes[0] * 10 + minute_bytes[1];
    index += 2;
    let Some(&hash) = bytes.get(index) else {
        return Err(invalid_creation_time(value));
    };
    if hash != b'#' {
        return Err(invalid_creation_time(value));
    }
    index += 1;
    if bytes.get(index) == Some(&b'#') {
        index += 1;
    }
    if index != bytes.len() {
        return Err(invalid_creation_time(value));
    }
    let Some(local_date) = Date::from_ymd(year as i32, month, day) else {
        return Err(invalid_creation_time(value));
    };
    if hour > 23 || minute > 59 {
        return Err(invalid_creation_time(value));
    }
    Ok(eastern_instant(local_date, hour, minute))
}

fn invalid_creation_time(value: &str) -> ReferenceError {
    ReferenceError::nasdaq(format!("invalid file creation time: {value:?}"))
}

/// Convert a US/Eastern wall clock to a UTC instant using the post-2007 US
/// DST rule, mirroring `lake.contracts.sessions`.
fn eastern_instant(local_date: Date, hour: u32, minute: u32) -> EasternInstant {
    let offset_seconds: i64 = if is_eastern_dst(local_date) {
        4 * 3_600
    } else {
        5 * 3_600
    };
    let local_seconds =
        local_date.days() as i64 * 86_400 + i64::from(hour) * 3_600 + i64::from(minute) * 60;
    EasternInstant {
        utc: DateTime::from_micros((local_seconds + offset_seconds) * 1_000_000),
        local_date,
        hour,
        minute,
        offset_seconds,
    }
}

fn is_eastern_dst(day: Date) -> bool {
    let (year, _, _) = day.ymd();
    day >= eastern_dst_start(year) && day < eastern_dst_end(year)
}

fn eastern_dst_start(year: i32) -> Date {
    nth_weekday(year, 3, 6, 2)
}

fn eastern_dst_end(year: i32) -> Date {
    nth_weekday(year, 11, 6, 1)
}

/// The `n`-th `weekday` (0 = Monday) of a month.
fn nth_weekday(year: i32, month: u32, weekday: u32, n: u32) -> Date {
    let first = Date::from_ymd(year, month, 1).expect("valid month start");
    let offset = (weekday as i32 - first.weekday_index() as i32).rem_euclid(7) + (n as i32 - 1) * 7;
    first.add_days(offset)
}
