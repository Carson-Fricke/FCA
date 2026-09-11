//! Synchronous market-lake access at the quant-harness boundary.
//!
//! Port of `harness.data.lake`. The adapter owns the provider-facing sequence
//! of ensure, job polling, and file query calls; the returned file objects,
//! including their API-issued read URLs, are passed to an injected reader. No
//! storage credentials or provider client are created by the harness.
//!
//! The XNYS session calendar is computed from the exchange holiday rules with
//! US Eastern DST applied; ad-hoc exchange closures are not modeled.

use std::cell::RefCell;
use std::fmt;
use std::rc::Rc;

use crate::error::DataError;
use crate::symbol::canonical_symbol;
use crate::time::{Date, DateTime};

/// Projected local reads at or above this byte count warn by default.
pub const DEFAULT_EGRESS_WARNING_THRESHOLD_BYTES: u64 = 1_000_000_000;

/// Supported Hive `asset_class` values.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum AssetClass {
    /// Listed equity.
    Equity,
    /// Listed option.
    Option,
    /// Crypto pair.
    Crypto,
    /// Reference data.
    Reference,
}

impl AssetClass {
    /// The Hive string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Equity => "equity",
            Self::Option => "option",
            Self::Crypto => "crypto",
            Self::Reference => "reference",
        }
    }

    /// Parse a Hive string value.
    pub fn from_value(value: &str) -> Result<Self, DataError> {
        match value {
            "equity" => Ok(Self::Equity),
            "option" => Ok(Self::Option),
            "crypto" => Ok(Self::Crypto),
            "reference" => Ok(Self::Reference),
            _ => Err(DataError::value(format!("unknown asset class: {value:?}"))),
        }
    }
}

impl fmt::Display for AssetClass {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.value())
    }
}

/// Supported Hive `data_type` values.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum DataType {
    /// Trade prints.
    Trade,
    /// Top-of-book quotes.
    Quote,
    /// Classification data.
    Classification,
    /// Fundamentals.
    Fundamentals,
}

impl DataType {
    /// The Hive string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Trade => "trade",
            Self::Quote => "quote",
            Self::Classification => "classification",
            Self::Fundamentals => "fundamentals",
        }
    }

    /// Parse a Hive string value.
    pub fn from_value(value: &str) -> Result<Self, DataError> {
        match value {
            "trade" => Ok(Self::Trade),
            "quote" => Ok(Self::Quote),
            "classification" => Ok(Self::Classification),
            "fundamentals" => Ok(Self::Fundamentals),
            _ => Err(DataError::value(format!("unknown data type: {value:?}"))),
        }
    }
}

impl fmt::Display for DataType {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.value())
    }
}

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

/// Percent-encode a canonical symbol for use as a Parquet filename.
pub fn symbol_filename(symbol: &str) -> Result<String, DataError> {
    let canonical = symbol.trim().to_uppercase();
    if canonical.is_empty()
        || canonical == "."
        || canonical == ".."
        || !canonical
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || "./_-".contains(c))
        || !canonical
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
    {
        return Err(DataError::value(format!(
            "invalid market symbol: {symbol:?}"
        )));
    }
    let mut encoded = String::new();
    for byte in canonical.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-') {
            encoded.push(byte as char);
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    Ok(format!("{encoded}.parquet"))
}

/// Build a repository-independent, date-first Hive object path.
pub fn partition_path(
    session_date: Date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> Result<String, DataError> {
    Ok(format!(
        "date={}/asset_class={}/data_type={}/{}",
        session_date.isoformat(),
        asset_class.value(),
        data_type.value(),
        symbol_filename(symbol)?
    ))
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

/// Weekday index with Monday = 0 through Sunday = 6.
fn weekday_index(day: Date) -> u32 {
    (day.days() + 3).rem_euclid(7) as u32
}

/// The `n`-th `weekday` (0 = Monday) of a month.
fn nth_weekday(year: i32, month: u32, weekday: u32, n: u32) -> Date {
    let first = Date::from_ymd(year, month, 1).expect("valid month start");
    let offset = (weekday as i32 - weekday_index(first) as i32).rem_euclid(7) + (n as i32 - 1) * 7;
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
    let delta = (weekday_index(last) as i32 - weekday as i32).rem_euclid(7);
    last.add_days(-delta)
}

fn is_session(day: Date) -> bool {
    if weekday_index(day) > 4 {
        return false;
    }
    let (year, month, day_of_month) = day.ymd();
    let holidays = nyse_holidays(year);
    !holidays
        .iter()
        .any(|(m, d)| *m == month && *d == day_of_month)
}

fn is_early_close(day: Date) -> bool {
    let (year, month, day_of_month) = day.ymd();
    // Independence Day eve (July 3) when it is a weekday.
    if month == 7 && day_of_month == 3 && weekday_index(day) <= 4 {
        return true;
    }
    // Thanksgiving is the 4th Thursday of November; the following Friday.
    let thanksgiving = nth_weekday(year, 11, 3, 4);
    day == thanksgiving.add_days(1)
}

fn observed(year: i32, month: u32, day: u32) -> (u32, u32) {
    let date = Date::from_ymd(year, month, day).expect("valid holiday");
    match weekday_index(date) {
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
    // New Year's Day of the next year lands on a Saturday: observed Dec 31.
    if Date::from_ymd(year + 1, 1, 1).map(weekday_index) == Some(5) {
        holidays.push((12, 31));
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

/// A deterministic, multi-symbol request for one lake dataset.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MarketLakeRequest {
    /// Asset class.
    pub asset_class: AssetClass,
    /// Data type.
    pub data_type: DataType,
    /// Canonical, sorted, de-duplicated symbols.
    pub symbols: Vec<String>,
    /// Inclusive start date.
    pub start_date: Date,
    /// Inclusive end date.
    pub end_date: Date,
}

impl MarketLakeRequest {
    /// Build a validated request.
    pub fn new(
        asset_class: AssetClass,
        data_type: DataType,
        symbols: Vec<String>,
        start_date: Date,
        end_date: Date,
    ) -> Result<Self, DataError> {
        if end_date < start_date {
            return Err(DataError::value("end_date must not be before start_date"));
        }
        let mut canonical = Vec::with_capacity(symbols.len());
        for symbol in &symbols {
            canonical.push(canonical_symbol(symbol)?);
        }
        canonical.sort();
        for window in canonical.windows(2) {
            if window[0] == window[1] {
                return Err(DataError::value("symbols must not contain duplicates"));
            }
        }
        Ok(Self {
            asset_class,
            data_type,
            symbols: canonical,
            start_date,
            end_date,
        })
    }
}

/// Resolve the request's exchange or UTC-day windows without using a clock.
pub fn resolve_sessions(request: &MarketLakeRequest) -> Result<Vec<ExchangeSession>, DataError> {
    match request.asset_class {
        AssetClass::Equity => xnys_sessions(request.start_date, request.end_date),
        AssetClass::Crypto => {
            let mut sessions = Vec::new();
            for session_date in crypto_utc_days(request.start_date, request.end_date)? {
                sessions.push(ExchangeSession {
                    session_date,
                    open_time: DateTime::midnight(session_date),
                    close_time: DateTime::midnight(session_date.add_days(1)),
                });
            }
            Ok(sessions)
        }
        other => Err(DataError::Unsupported(format!(
            "market-lake adapter does not support {} data",
            other.value()
        ))),
    }
}

/// One bounded symbol/data-type request handed to the provider client.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DatasetRequest {
    /// Asset class.
    pub asset_class: AssetClass,
    /// Data type.
    pub data_type: DataType,
    /// Single symbol, if scoped.
    pub symbol: Option<String>,
    /// Inclusive start date.
    pub start_date: Date,
    /// Inclusive end date.
    pub end_date: Date,
}

/// Terminal and replaceable outcomes for one symbol-day coverage row.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoverageState {
    /// Complete.
    Complete,
    /// No data.
    Empty,
    /// Partial.
    Partial,
    /// Error.
    Error,
}

/// One coverage row returned by an ensure call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoverageEntry {
    /// Session date.
    pub session_date: Date,
    /// UTC session open.
    pub open_time: DateTime,
    /// UTC session close.
    pub close_time: DateTime,
    /// Coverage status.
    pub status: CoverageState,
    /// Whether the row is partial.
    pub is_partial: bool,
    /// Canonical object path, if any.
    pub object_path: Option<String>,
}

/// Validated coverage for one ensure call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoverageResponse {
    /// Asset class.
    pub asset_class: AssetClass,
    /// Data type.
    pub data_type: DataType,
    /// Symbol, if scoped.
    pub symbol: Option<String>,
    /// Inclusive start date.
    pub start_date: Date,
    /// Inclusive end date.
    pub end_date: Date,
    /// Per-session coverage.
    pub entries: Vec<CoverageEntry>,
}

/// An accepted asynchronous ensure job.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnsureAccepted {
    /// Job identifier.
    pub job_id: String,
}

/// The response to an ensure call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnsureResponse {
    /// Synchronous coverage.
    Coverage(CoverageResponse),
    /// Asynchronous job.
    Accepted(EnsureAccepted),
}

/// Terminal state of an ensure job.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobStatus {
    /// Whether the job succeeded.
    pub succeeded: bool,
    /// Raw provider state value.
    pub state: String,
    /// Provider error message, if any.
    pub error_message: Option<String>,
}

/// Query completeness.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FileQueryState {
    /// All requested sessions returned.
    Complete,
    /// Some sessions are still pending.
    Incomplete,
}

/// Per-request file query summary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQuerySummary {
    /// Completeness.
    pub state: FileQueryState,
    /// Pending session count.
    pub pending_sessions: usize,
    /// Requested session count.
    pub requested_sessions: usize,
    /// Complete session count.
    pub complete_sessions: usize,
    /// Returned file count.
    pub returned_files: usize,
}

/// One complete file with its API-issued read URL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQueryFile {
    /// Asset class.
    pub asset_class: AssetClass,
    /// Data type.
    pub data_type: DataType,
    /// Symbol.
    pub symbol: String,
    /// Session date.
    pub session_date: Date,
    /// Canonical object path.
    pub object_path: String,
    /// API-issued read URL.
    pub read_url: String,
    /// Written byte size.
    pub byte_size: u64,
    /// Written row count.
    pub row_count: u64,
}

/// The response to a file query call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQueryResponse {
    /// Query summary.
    pub results: FileQuerySummary,
    /// Complete, readable files.
    pub files: Vec<FileQueryFile>,
}

/// Published lakeclient operations used by the harness adapter.
pub trait LakeClientPort {
    /// Ensure one bounded symbol/data-type request.
    fn ensure(&self, request: &DatasetRequest) -> Result<EnsureResponse, DataError>;
    /// Poll one accepted ensure job to a terminal state.
    fn wait_for_job(
        &self,
        job_id: &str,
        timeout: f64,
        poll_interval: f64,
    ) -> Result<JobStatus, DataError>;
    /// Return complete files and API-issued read URLs.
    fn query_files(
        &self,
        request: &DatasetRequest,
        ensure: bool,
    ) -> Result<FileQueryResponse, DataError>;
}

/// A shared lake client.
pub type ClientRef = Rc<dyn LakeClientPort>;
/// A lazily resolved lake-client factory.
pub type ClientFactory = Rc<dyn Fn() -> ClientRef>;
/// Injected reader over validated lake files.
pub type FileReader<R> = Box<dyn Fn(&[FileQueryFile]) -> R>;

/// Reader output and validated file metadata for one prepared load.
#[derive(Clone, Debug)]
pub struct MarketLakeLoad<R> {
    /// The resolved request.
    pub request: MarketLakeRequest,
    /// Resolved exchange sessions.
    pub sessions: Vec<ExchangeSession>,
    /// Ordered, validated files.
    pub files: Vec<FileQueryFile>,
    /// The injected reader's output.
    pub data: R,
    /// Total projected bytes.
    pub projected_bytes: u64,
}

/// Prepare deterministic harness data through the published lakeclient API.
pub struct MarketLakeAdapter<R> {
    client: RefCell<Option<ClientRef>>,
    client_factory: Option<ClientFactory>,
    reader: FileReader<R>,
    warning: Box<dyn Fn(&str)>,
    egress_warning_threshold_bytes: Option<u64>,
    job_timeout: f64,
    job_poll_interval: f64,
}

impl<R> MarketLakeAdapter<R> {
    /// Build an adapter with an explicit client.
    pub fn with_client(
        client: ClientRef,
        reader: FileReader<R>,
        egress_warning_threshold_bytes: Option<u64>,
    ) -> Result<Self, DataError> {
        Self::build(
            Some(client),
            None,
            reader,
            egress_warning_threshold_bytes,
            60.0,
            0.5,
        )
    }

    /// Build an adapter with a lazily resolved client factory.
    pub fn with_client_factory(
        factory: ClientFactory,
        reader: FileReader<R>,
        egress_warning_threshold_bytes: Option<u64>,
    ) -> Result<Self, DataError> {
        Self::build(
            None,
            Some(factory),
            reader,
            egress_warning_threshold_bytes,
            60.0,
            0.5,
        )
    }

    fn build(
        client: Option<ClientRef>,
        factory: Option<ClientFactory>,
        reader: FileReader<R>,
        egress_warning_threshold_bytes: Option<u64>,
        job_timeout: f64,
        job_poll_interval: f64,
    ) -> Result<Self, DataError> {
        if client.is_some() == factory.is_some() {
            return Err(DataError::type_error(
                "exactly one of client or client_factory must be provided",
            ));
        }
        if job_timeout.is_nan() || job_timeout < 0.0 {
            return Err(DataError::value("job_timeout must not be negative"));
        }
        if job_poll_interval.is_nan() || job_poll_interval < 0.0 {
            return Err(DataError::value("job_poll_interval must not be negative"));
        }
        Ok(Self {
            client: RefCell::new(client),
            client_factory: factory,
            reader,
            warning: Box::new(default_warning),
            egress_warning_threshold_bytes,
            job_timeout,
            job_poll_interval,
        })
    }

    /// Override the warning sink.
    pub fn set_warning(&mut self, warning: Box<dyn Fn(&str)>) {
        self.warning = warning;
    }

    fn resolved_client(&self) -> Result<ClientRef, DataError> {
        if let Some(client) = self.client.borrow().as_ref() {
            return Ok(Rc::clone(client));
        }
        let factory = self
            .client_factory
            .as_ref()
            .ok_or_else(|| DataError::value("market-lake adapter has no lake client"))?;
        let client = factory();
        *self.client.borrow_mut() = Some(Rc::clone(&client));
        Ok(client)
    }

    /// Resolve exact XNYS or UTC session windows for a request.
    pub fn resolve_sessions(
        &self,
        request: &MarketLakeRequest,
    ) -> Result<Vec<ExchangeSession>, DataError> {
        resolve_sessions(request)
    }

    /// Ensure, validate, and read all files needed for one harness load.
    pub fn load(&self, request: &MarketLakeRequest) -> Result<MarketLakeLoad<R>, DataError> {
        if !matches!(request.asset_class, AssetClass::Equity | AssetClass::Crypto) {
            return Err(DataError::Unsupported(format!(
                "market-lake adapter does not support {} data",
                request.asset_class.value()
            )));
        }
        let sessions = resolve_sessions(request)?;
        if sessions.is_empty() {
            return Err(DataError::Unsupported(
                "requested range contains no supported market sessions".to_string(),
            ));
        }
        let requests: Vec<DatasetRequest> = request
            .symbols
            .iter()
            .map(|symbol| DatasetRequest {
                asset_class: request.asset_class,
                data_type: request.data_type,
                symbol: Some(symbol.clone()),
                start_date: request.start_date,
                end_date: request.end_date,
            })
            .collect();
        for dataset_request in &requests {
            self.ensure(dataset_request, &sessions)?;
        }
        let mut files = Vec::new();
        for dataset_request in &requests {
            files.extend(self.query(dataset_request, &sessions)?);
        }
        files.sort_by(|a, b| {
            (&a.symbol, a.session_date, &a.object_path).cmp(&(
                &b.symbol,
                b.session_date,
                &b.object_path,
            ))
        });
        let projected_bytes: u64 = files.iter().map(|file| file.byte_size).sum();
        if let Some(threshold) = self.egress_warning_threshold_bytes {
            if projected_bytes >= threshold {
                (self.warning)(&format!(
                    "projected local market-lake egress ({projected_bytes} bytes) meets or \
                     exceeds the configured threshold ({threshold} bytes)"
                ));
            }
        }
        let data = (self.reader)(&files);
        Ok(MarketLakeLoad {
            request: request.clone(),
            sessions,
            files,
            data,
            projected_bytes,
        })
    }

    fn ensure(
        &self,
        request: &DatasetRequest,
        sessions: &[ExchangeSession],
    ) -> Result<(), DataError> {
        let client = self.resolved_client()?;
        match client.ensure(request)? {
            EnsureResponse::Accepted(accepted) => {
                let status = client.wait_for_job(
                    &accepted.job_id,
                    self.job_timeout,
                    self.job_poll_interval,
                )?;
                if !status.succeeded {
                    let detail = status.error_message.unwrap_or(status.state);
                    return Err(DataError::EnsureJob(format!(
                        "market-lake ensure job {:?} failed: {detail}",
                        accepted.job_id
                    )));
                }
                Ok(())
            }
            EnsureResponse::Coverage(response) => {
                validate_coverage_response(&response, request, sessions)
            }
        }
    }

    fn query(
        &self,
        request: &DatasetRequest,
        sessions: &[ExchangeSession],
    ) -> Result<Vec<FileQueryFile>, DataError> {
        let response = self.resolved_client()?.query_files(request, false)?;
        validate_file_response(&response, request, sessions)
    }
}

fn default_warning(_message: &str) {}

fn validate_coverage_response(
    response: &CoverageResponse,
    request: &DatasetRequest,
    sessions: &[ExchangeSession],
) -> Result<(), DataError> {
    if response.asset_class != request.asset_class {
        return Err(DataError::value(
            "lakeclient ensure response has an unexpected asset class",
        ));
    }
    if response.data_type != request.data_type {
        return Err(DataError::value(
            "lakeclient ensure response has an unexpected data type",
        ));
    }
    if response.start_date != request.start_date || response.end_date != request.end_date {
        return Err(DataError::value(
            "lakeclient ensure response has an unexpected date range",
        ));
    }
    if let (Some(symbol), Some(request_symbol)) = (&response.symbol, &request.symbol) {
        if symbol.trim().to_uppercase() != *request_symbol {
            return Err(DataError::value(
                "lakeclient ensure response has an unexpected symbol",
            ));
        }
    }
    let expected: std::collections::HashMap<Date, &ExchangeSession> = sessions
        .iter()
        .map(|session| (session.session_date, session))
        .collect();
    let mut entries_by_date = std::collections::HashMap::new();
    for entry in &response.entries {
        let Some(expected_session) = expected.get(&entry.session_date) else {
            return Err(DataError::Incomplete(format!(
                "ensure returned an unexpected session for {:?}",
                request.symbol
            )));
        };
        if entries_by_date.insert(entry.session_date, entry).is_some() {
            return Err(DataError::Incomplete(format!(
                "ensure returned duplicate coverage for {:?}",
                request.symbol
            )));
        }
        if entry.open_time != expected_session.open_time
            || entry.close_time != expected_session.close_time
        {
            return Err(DataError::value(format!(
                "ensure returned an unexpected XNYS window for {:?} on {}",
                request.symbol,
                entry.session_date.isoformat()
            )));
        }
        if entry.status != CoverageState::Complete || entry.is_partial {
            return Err(DataError::Incomplete(format!(
                "coverage remains incomplete for {:?} on {}",
                request.symbol,
                entry.session_date.isoformat()
            )));
        }
        if entry.object_path.as_deref()
            != Some(expected_path(request, entry.session_date)?.as_str())
        {
            return Err(DataError::Incomplete(format!(
                "coverage has no canonical object path for {:?} on {}",
                request.symbol,
                entry.session_date.isoformat()
            )));
        }
    }
    for session in sessions {
        if !entries_by_date.contains_key(&session.session_date) {
            return Err(DataError::Incomplete(format!(
                "ensure omitted complete coverage for {:?}: {}",
                request.symbol,
                session.session_date.isoformat()
            )));
        }
    }
    Ok(())
}

fn validate_file_response(
    response: &FileQueryResponse,
    request: &DatasetRequest,
    sessions: &[ExchangeSession],
) -> Result<Vec<FileQueryFile>, DataError> {
    let expected_dates: std::collections::HashSet<Date> = sessions
        .iter()
        .map(|session| session.session_date)
        .collect();
    let expected_count = expected_dates.len();
    let results = &response.results;
    if results.state != FileQueryState::Complete || results.pending_sessions != 0 {
        return Err(DataError::Incomplete(format!(
            "file query remains incomplete for {:?}",
            request.symbol
        )));
    }
    if results.requested_sessions != expected_count
        || results.complete_sessions != expected_count
        || results.returned_files != expected_count
    {
        return Err(DataError::Incomplete(format!(
            "file query returned incomplete session counts for {:?}",
            request.symbol
        )));
    }
    if response.files.len() != expected_count {
        return Err(DataError::Incomplete(format!(
            "file query returned an unexpected file count for {:?}",
            request.symbol
        )));
    }
    let mut seen = std::collections::HashSet::new();
    for file in &response.files {
        let symbol_matches = request
            .symbol
            .as_ref()
            .is_some_and(|symbol| file.symbol.trim().to_uppercase() == *symbol);
        if file.asset_class != request.asset_class
            || file.data_type != request.data_type
            || !symbol_matches
        {
            return Err(DataError::value(
                "file query returned a file outside the requested dataset",
            ));
        }
        if !expected_dates.contains(&file.session_date) || !seen.insert(file.session_date) {
            return Err(DataError::Incomplete(format!(
                "file query returned an unexpected or duplicate session for {:?}",
                request.symbol
            )));
        }
        if file.object_path != expected_path(request, file.session_date)? {
            return Err(DataError::value(
                "file query returned a non-canonical object path",
            ));
        }
        if file.read_url.trim().is_empty() {
            return Err(DataError::value(
                "file query returned an empty API-issued read URL",
            ));
        }
        seen.insert(file.session_date);
    }
    if seen.len() != expected_count {
        return Err(DataError::Incomplete(format!(
            "file query omitted requested sessions for {:?}",
            request.symbol
        )));
    }
    Ok(response.files.clone())
}

fn expected_path(request: &DatasetRequest, session_date: Date) -> Result<String, DataError> {
    let symbol = request
        .symbol
        .as_ref()
        .ok_or_else(|| DataError::type_error("a symbol is required to build a partition path"))?;
    partition_path(session_date, request.asset_class, request.data_type, symbol)
}

#[cfg(test)]
#[path = "../tests/unit/lake.rs"]
mod tests;
