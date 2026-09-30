//! Provider-independent catalog value objects.
//!
//! Ports `lake/catalog/models.py`: every record validates on construction with
//! the Python wording, exposes the same deterministic keys, and persists under
//! the exact JSON key names Python uses.

use std::collections::BTreeMap;
use std::fmt;
use std::fmt::Write as _;

use serde::de::{self, Deserializer};
use serde::ser::{SerializeStruct, Serializer};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use lake_contracts::{
    canonical_symbol, AssetClass, DataError, DataType, Date, DateTime, Decimal18,
};

const TABLE_KEY_SAFE: &[u8] = b"_.-~";

/// The exact JSON key set of one legacy coverage row in `catalog.json`.
pub(crate) const COVERAGE_JSON_KEYS: [&str; 10] = [
    "asset_class",
    "byte_size",
    "checksum",
    "data_type",
    "error_message",
    "object_path",
    "row_count",
    "session_date",
    "status",
    "symbol",
];

/// The optional JSON key carrying a bars interval; absent for every other row.
pub(crate) const COVERAGE_INTERVAL_JSON_KEY: &str = "interval";

/// The optional JSON key carrying the bars provenance source.
pub(crate) const COVERAGE_BAR_SOURCE_JSON_KEY: &str = "bar_source";

/// The optional JSON key carrying the bars source revision.
pub(crate) const COVERAGE_SOURCE_REVISION_JSON_KEY: &str = "source_revision";

/// Encode a value for use in Azure Table keys without changing its identity.
///
/// Mirrors Python's `urllib.parse.quote(value, safe="._-~")`: bytes outside
/// ASCII letters, digits, and `._-~` are percent-encoded as uppercase hex.
pub fn table_key(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || TABLE_KEY_SAFE.contains(byte) {
            encoded.push(char::from(*byte));
        } else {
            let _ = write!(encoded, "%{byte:02X}");
        }
    }
    encoded
}

macro_rules! persisted_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident => $value:literal,)+ }) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub enum $name {
            $(
                $variant,
            )+
        }

        impl $name {
            /// The persisted string value.
            pub const fn value(self) -> &'static str {
                match self {
                    $(Self::$variant => $value,)+
                }
            }

            /// Parse the persisted string value.
            pub fn from_value(value: &str) -> Option<Self> {
                match value {
                    $($value => Some(Self::$variant),)+
                    _ => None,
                }
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(self.value())
            }
        }

        impl Serialize for $name {
            fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
            where
                S: Serializer,
            {
                serializer.serialize_str(self.value())
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
            where
                D: Deserializer<'de>,
            {
                let value = String::deserialize(deserializer)?;
                Self::from_value(&value).ok_or_else(|| {
                    de::Error::custom(format_args!(
                        "{value:?} is not a valid {}",
                        stringify!($name)
                    ))
                })
            }
        }
    };
}

persisted_enum! {
    /// Terminal and replaceable outcomes for one symbol-day dataset.
    CoverageStatus {
        Complete => "complete",
        Empty => "empty",
        Partial => "partial",
        Error => "error",
    }
}

persisted_enum! {
    /// How one bars partition was produced.
    BarSource {
        Provider => "provider",
        Derived => "derived",
    }
}

persisted_enum! {
    /// States used by asynchronous ensure and backfill jobs.
    JobState {
        Queued => "queued",
        Running => "running",
        Complete => "complete",
        Succeeded => "succeeded",
        Failed => "failed",
        Cancelled => "cancelled",
    }
}

persisted_enum! {
    /// Lifecycle states for one stored backtest run.
    RunState {
        Queued => "queued",
        Running => "running",
        Complete => "complete",
        Failed => "failed",
    }
}

persisted_enum! {
    /// The side of an option contract.
    OptionType {
        Call => "call",
        Put => "put",
    }
}

persisted_enum! {
    /// How one entity came to be listed under a symbol.
    ListingType {
        Ipo => "ipo",
        Direct => "direct",
        Spinoff => "spinoff",
        Rename => "rename",
        Transfer => "transfer",
        Unknown => "unknown",
    }
}

persisted_enum! {
    /// Why one listing episode ended.
    DelistingType {
        Rename => "rename",
        Merger => "merger",
        Acquisition => "acquisition",
        Bankruptcy => "bankruptcy",
        Voluntary => "voluntary",
        Regulatory => "regulatory",
        Unknown => "unknown",
    }
}

/// Serde support for a required [`Date`] rendered as `YYYY-MM-DD`.
pub(crate) mod date_format {
    use lake_contracts::Date;
    use serde::{Deserialize, Deserializer};

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Date, D::Error>
    where
        D: Deserializer<'de>,
    {
        let text = String::deserialize(deserializer)?;
        Date::parse(&text)
            .ok_or_else(|| serde::de::Error::custom(format_args!("invalid date: {text:?}")))
    }
}

/// Serde support for an optional [`Date`] rendered as `YYYY-MM-DD` or `null`.
pub(crate) mod optional_date {
    use lake_contracts::Date;
    use serde::{Deserialize, Deserializer};

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Option<Date>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let text = Option::<String>::deserialize(deserializer)?;
        match text {
            Some(text) => Date::parse(&text)
                .map(Some)
                .ok_or_else(|| serde::de::Error::custom(format_args!("invalid date: {text:?}"))),
            None => Ok(None),
        }
    }
}

/// Serde support for a required [`DateTime`] rendered as UTC ISO-8601.
pub(crate) mod datetime_format {
    use lake_contracts::DateTime;
    use serde::{Deserialize, Deserializer};

    pub fn deserialize<'de, D>(deserializer: D) -> Result<DateTime, D::Error>
    where
        D: Deserializer<'de>,
    {
        let text = String::deserialize(deserializer)?;
        DateTime::parse_iso_utc(&text)
            .ok_or_else(|| serde::de::Error::custom(format_args!("invalid datetime: {text:?}")))
    }
}

/// Coverage metadata for one asset, data type, symbol, and session date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoverageRecord {
    /// Asset class of the covered partition.
    pub asset_class: AssetClass,
    /// Data type of the covered partition.
    pub data_type: DataType,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Session the coverage row describes.
    pub session_date: Date,
    /// Terminal or replaceable coverage outcome.
    pub status: CoverageStatus,
    /// Relative object path of the written partition, if any.
    pub object_path: Option<String>,
    /// Rows written for this symbol-day.
    pub row_count: u64,
    /// Bytes written for this symbol-day.
    pub byte_size: u64,
    /// Content digest of the written object, if any.
    pub checksum: Option<String>,
    /// Failure detail when the status is `error`.
    pub error_message: Option<String>,
    /// Canonical bar interval token for bars partitions; `None` otherwise.
    pub interval: Option<String>,
    /// How a bars partition was produced; `None` for every non-bars row and
    /// for legacy bars rows written before provenance existed.
    pub bar_source: Option<BarSource>,
    /// The revision the bars partition was built from: the provider response
    /// revision for fetched bars and the trade partition revision for derived
    /// bars. Present exactly when `bar_source` is.
    pub source_revision: Option<String>,
}

impl CoverageRecord {
    /// Build a validated coverage row.
    ///
    /// The symbol is canonicalised and empty symbols are rejected, mirroring
    /// Python's `canonical_symbol`. Counts are unsigned, so they can never be
    /// negative, and a session date can never be a `datetime`. Bars rows must
    /// carry exactly one canonical interval token; every other data type must
    /// not carry one.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        asset_class: AssetClass,
        data_type: DataType,
        symbol: impl Into<String>,
        session_date: Date,
        status: CoverageStatus,
        object_path: Option<String>,
        row_count: u64,
        byte_size: u64,
        checksum: Option<String>,
        error_message: Option<String>,
        interval: Option<String>,
    ) -> Result<Self, DataError> {
        let interval = match (data_type, interval) {
            (DataType::Bars, Some(interval)) => {
                lake_contracts::BarInterval::parse(&interval)?;
                Some(interval)
            }
            (DataType::Bars, None) => {
                return Err(DataError::value("bars coverage requires an interval"));
            }
            (_, Some(_)) => {
                return Err(DataError::value(
                    "coverage interval is supported for bars only",
                ));
            }
            (_, None) => None,
        };
        Ok(Self {
            asset_class,
            data_type,
            symbol: canonical_symbol(&symbol.into())?,
            session_date,
            status,
            object_path,
            row_count,
            byte_size,
            checksum,
            error_message,
            interval,
            bar_source: None,
            source_revision: None,
        })
    }

    /// Attach bars provenance to a coverage row.
    ///
    /// Only bars rows carry a source, and a source always names the revision
    /// it was built from: `provider` rows record the fetched partition
    /// revision and `derived` rows record the trade partition revision, so a
    /// later trade repair can be detected as a revision mismatch. Rows without
    /// provenance stay byte-identical to the legacy serialization.
    pub fn with_bar_provenance(
        mut self,
        source: BarSource,
        revision: impl Into<String>,
    ) -> Result<Self, DataError> {
        if self.data_type != DataType::Bars {
            return Err(DataError::value(
                "bar source is supported for bars coverage only",
            ));
        }
        let revision = revision.into();
        if revision.trim().is_empty() {
            return Err(DataError::value("bar source requires a source revision"));
        }
        self.bar_source = Some(source);
        self.source_revision = Some(revision);
        Ok(self)
    }

    /// The deterministic logical key for this coverage row.
    pub fn key(&self) -> (AssetClass, DataType, String, Option<String>, Date) {
        (
            self.asset_class,
            self.data_type,
            self.symbol.clone(),
            self.interval.clone(),
            self.session_date,
        )
    }

    /// The Azure Table partition key shared by memory and Table adapters.
    ///
    /// Bars rows append their interval so two intervals of the same
    /// symbol-day never collide; every other row keeps the legacy shape.
    pub fn partition_key(&self) -> String {
        let base = format!(
            "coverage|{}|{}|{}",
            self.asset_class.value(),
            self.data_type.value(),
            table_key(&self.symbol)
        );
        match &self.interval {
            Some(interval) => format!("{base}|interval={}", table_key(interval)),
            None => base,
        }
    }

    /// The Azure Table row key: the ISO session date, plus the bars interval.
    pub fn row_key(&self) -> String {
        match &self.interval {
            Some(interval) => format!(
                "{}|interval={}",
                self.session_date.isoformat(),
                table_key(interval)
            ),
            None => self.session_date.isoformat(),
        }
    }

    /// Whether the status is terminal: `complete` or `empty`.
    pub fn is_terminal(&self) -> bool {
        matches!(
            self.status,
            CoverageStatus::Complete | CoverageStatus::Empty
        )
    }

    /// Whether a persisted `partial` row should be replaced by a fetch.
    pub fn replace_existing(&self) -> bool {
        self.status == CoverageStatus::Partial
    }

    pub(crate) fn sort_key(&self) -> (&'static str, &'static str, &str, Option<&str>, Date) {
        (
            self.asset_class.value(),
            self.data_type.value(),
            self.symbol.as_str(),
            self.interval.as_deref(),
            self.session_date,
        )
    }
}

/// Restore one coverage row from a parsed `catalog.json` row value.
pub(crate) fn coverage_from_value(value: &Value) -> Result<CoverageRecord, DataError> {
    let object = value
        .as_object()
        .ok_or_else(|| DataError::value("coverage row has unexpected fields"))?;
    let expected_keys = COVERAGE_JSON_KEYS.len()
        + usize::from(object.contains_key(COVERAGE_INTERVAL_JSON_KEY))
        + usize::from(object.contains_key(COVERAGE_BAR_SOURCE_JSON_KEY))
        + usize::from(object.contains_key(COVERAGE_SOURCE_REVISION_JSON_KEY));
    if object.len() != expected_keys
        || !COVERAGE_JSON_KEYS
            .iter()
            .all(|key| object.contains_key(*key))
    {
        return Err(DataError::value("coverage row has unexpected fields"));
    }
    let session_date = object["session_date"]
        .as_str()
        .ok_or_else(|| DataError::type_error("coverage session_date must be a string"))?;
    let session_date = Date::parse(session_date)
        .ok_or_else(|| DataError::value(format!("Invalid isoformat string: {session_date:?}")))?;
    let symbol = object["symbol"]
        .as_str()
        .ok_or_else(|| DataError::type_error("coverage symbol must be a string"))?;
    let row_count = coverage_integer(object, "row_count")?;
    let byte_size = coverage_integer(object, "byte_size")?;
    let object_path = coverage_optional_string(object, "object_path")?;
    let checksum = coverage_optional_string(object, "checksum")?;
    let error_message = coverage_optional_string(object, "error_message")?;
    let interval = coverage_interval(object)?;
    let bar_source = coverage_bar_source(object)?;
    let source_revision = coverage_optional_key_string(object, COVERAGE_SOURCE_REVISION_JSON_KEY)?;
    let asset_class =
        coverage_enum::<AssetClass>(object, "asset_class", "AssetClass", AssetClass::from_value)?;
    let data_type =
        coverage_enum::<DataType>(object, "data_type", "DataType", DataType::from_value)?;
    let status = coverage_enum::<CoverageStatus>(
        object,
        "status",
        "CoverageStatus",
        CoverageStatus::from_value,
    )?;
    let record = CoverageRecord::new(
        asset_class,
        data_type,
        symbol.to_string(),
        session_date,
        status,
        object_path,
        row_count,
        byte_size,
        checksum,
        error_message,
        interval,
    )?;
    match bar_source {
        Some(source) => {
            let revision = source_revision.ok_or_else(|| {
                DataError::value("coverage bar_source requires a source_revision")
            })?;
            record.with_bar_provenance(source, revision)
        }
        None => {
            if source_revision.is_some() {
                return Err(DataError::value(
                    "coverage source_revision requires a bar_source",
                ));
            }
            Ok(record)
        }
    }
}

fn coverage_bar_source(object: &Map<String, Value>) -> Result<Option<BarSource>, DataError> {
    if !object.contains_key(COVERAGE_BAR_SOURCE_JSON_KEY) {
        return Ok(None);
    }
    coverage_enum::<BarSource>(
        object,
        COVERAGE_BAR_SOURCE_JSON_KEY,
        "BarSource",
        BarSource::from_value,
    )
    .map(Some)
}

fn coverage_optional_key_string(
    object: &Map<String, Value>,
    name: &str,
) -> Result<Option<String>, DataError> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(DataError::type_error(format!(
            "coverage {name} must be a string or null"
        ))),
    }
}

fn coverage_interval(object: &Map<String, Value>) -> Result<Option<String>, DataError> {
    match object.get(COVERAGE_INTERVAL_JSON_KEY) {
        None => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(DataError::type_error("coverage interval must be a string")),
    }
}

fn coverage_integer(object: &Map<String, Value>, name: &str) -> Result<u64, DataError> {
    object[name]
        .as_u64()
        .ok_or_else(|| DataError::type_error(format!("coverage {name} must be an integer")))
}

fn coverage_optional_string(
    object: &Map<String, Value>,
    name: &str,
) -> Result<Option<String>, DataError> {
    match &object[name] {
        Value::Null => Ok(None),
        Value::String(text) => Ok(Some(text.clone())),
        _ => Err(DataError::type_error(format!(
            "coverage {name} must be a string or null"
        ))),
    }
}

fn coverage_enum<T>(
    object: &Map<String, Value>,
    name: &str,
    type_name: &str,
    parse: impl Fn(&str) -> Option<T>,
) -> Result<T, DataError> {
    let text = object[name]
        .as_str()
        .ok_or_else(|| DataError::type_error(format!("coverage {name} must be a string")))?;
    parse(text).ok_or_else(|| DataError::value(format!("{text:?} is not a valid {type_name}")))
}

impl Serialize for CoverageRecord {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let fields = COVERAGE_JSON_KEYS.len()
            + usize::from(self.interval.is_some())
            + usize::from(self.bar_source.is_some())
            + usize::from(self.source_revision.is_some());
        let mut state = serializer.serialize_struct("CoverageRecord", fields)?;
        state.serialize_field("asset_class", self.asset_class.value())?;
        if let Some(bar_source) = &self.bar_source {
            state.serialize_field(COVERAGE_BAR_SOURCE_JSON_KEY, bar_source.value())?;
        }
        state.serialize_field("byte_size", &self.byte_size)?;
        state.serialize_field("checksum", &self.checksum)?;
        state.serialize_field("data_type", self.data_type.value())?;
        state.serialize_field("error_message", &self.error_message)?;
        if let Some(interval) = &self.interval {
            state.serialize_field(COVERAGE_INTERVAL_JSON_KEY, interval)?;
        }
        state.serialize_field("object_path", &self.object_path)?;
        state.serialize_field("row_count", &self.row_count)?;
        state.serialize_field("session_date", &self.session_date.isoformat())?;
        if let Some(source_revision) = &self.source_revision {
            state.serialize_field(COVERAGE_SOURCE_REVISION_JSON_KEY, source_revision)?;
        }
        state.serialize_field("status", self.status.value())?;
        state.serialize_field("symbol", &self.symbol)?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for CoverageRecord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Value::deserialize(deserializer)?;
        coverage_from_value(&value).map_err(de::Error::custom)
    }
}

/// Incremental progress for one long-running ensure job.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobProgress {
    /// Sessions the job plans to fill.
    pub sessions_total: u64,
    /// Sessions that reached a terminal outcome so far.
    pub sessions_done: u64,
    /// Symbol of the session that just finished.
    pub symbol: String,
    /// Session date that just finished, ISO formatted.
    pub session_date: String,
    /// Rows written for the finished session.
    pub rows: u64,
    /// Failure detail when the finished session failed.
    pub error: Option<String>,
}

/// State and audit timestamps for one catalog/ensure job.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobRecord {
    /// Stable job identifier.
    pub job_id: String,
    /// Current job state.
    pub state: JobState,
    /// UTC creation instant.
    pub created_at: DateTime,
    /// UTC instant of the last state change.
    pub updated_at: DateTime,
    /// Failure detail when the job failed.
    pub error_message: Option<String>,
    /// Incremental progress while the job runs; absent before the first
    /// session finishes.
    pub progress: Option<JobProgress>,
}

impl JobRecord {
    /// Build a validated job record, trimming the job id like Python does.
    pub fn new(
        job_id: impl Into<String>,
        state: JobState,
        created_at: DateTime,
        updated_at: DateTime,
        error_message: Option<String>,
    ) -> Result<Self, DataError> {
        let job_id = job_id.into().trim().to_string();
        if job_id.is_empty() {
            return Err(DataError::value("job_id must not be empty"));
        }
        Ok(Self {
            job_id,
            state,
            created_at,
            updated_at,
            error_message,
            progress: None,
        })
    }

    /// Attach one incremental progress snapshot.
    #[must_use]
    pub fn with_progress(mut self, progress: JobProgress) -> Self {
        self.progress = Some(progress);
        self
    }

    /// The Azure Table row key for this job.
    pub fn row_key(&self) -> String {
        table_key(&self.job_id)
    }
}

#[derive(Deserialize)]
struct JobJson {
    job_id: String,
    state: JobState,
    #[serde(with = "datetime_format")]
    created_at: DateTime,
    #[serde(with = "datetime_format")]
    updated_at: DateTime,
    #[serde(default)]
    error_message: Option<String>,
    #[serde(default)]
    progress: Option<JobProgress>,
}

impl Serialize for JobRecord {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("JobRecord", 6)?;
        state.serialize_field("created_at", &self.created_at.isoformat_micros_utc())?;
        state.serialize_field("error_message", &self.error_message)?;
        state.serialize_field("job_id", &self.job_id)?;
        state.serialize_field("progress", &self.progress)?;
        state.serialize_field("state", &self.state)?;
        state.serialize_field("updated_at", &self.updated_at.isoformat_micros_utc())?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for JobRecord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = JobJson::deserialize(deserializer)?;
        let mut record = JobRecord::new(
            raw.job_id,
            raw.state,
            raw.created_at,
            raw.updated_at,
            raw.error_message,
        )
        .map_err(de::Error::custom)?;
        record.progress = raw.progress;
        Ok(record)
    }
}

/// Catalog row for one stored backtest run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunRecord {
    /// Stable run identifier.
    pub run_id: String,
    /// Lifecycle state of the run artifact.
    pub status: RunState,
    /// Strategy that produced the run.
    pub strategy_id: String,
    /// Inclusive first data date, if the run declared one.
    pub data_start: Option<Date>,
    /// Inclusive last data date, if the run declared one.
    pub data_end: Option<Date>,
    /// Digest over the run artifact contents.
    pub run_digest: Option<String>,
    /// Digest over the run configuration.
    pub configuration_digest: Option<String>,
    /// Number of stored artifacts.
    pub artifact_count: u64,
    /// Total size of the stored artifacts.
    pub total_bytes: u64,
    /// String-exact run metrics.
    pub metrics: BTreeMap<String, String>,
}

impl RunRecord {
    /// Build a validated run record.
    ///
    /// The run and strategy identifiers are trimmed and must be non-empty, the
    /// date bounds must be ordered, and counts are unsigned. Mirrors Python's
    /// `RunRecord.__post_init__`.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        run_id: impl Into<String>,
        status: RunState,
        strategy_id: impl Into<String>,
        data_start: Option<Date>,
        data_end: Option<Date>,
        run_digest: Option<String>,
        configuration_digest: Option<String>,
        artifact_count: u64,
        total_bytes: u64,
        metrics: BTreeMap<String, String>,
    ) -> Result<Self, DataError> {
        let run_id = run_id.into().trim().to_string();
        if run_id.is_empty() {
            return Err(DataError::value("run_id must not be empty"));
        }
        let strategy_id = strategy_id.into().trim().to_string();
        if strategy_id.is_empty() {
            return Err(DataError::value("strategy_id must not be empty"));
        }
        if let (Some(start), Some(end)) = (data_start, data_end) {
            if start > end {
                return Err(DataError::value("data_start must be on or before data_end"));
            }
        }
        Ok(Self {
            run_id,
            status,
            strategy_id,
            data_start,
            data_end,
            run_digest,
            configuration_digest,
            artifact_count,
            total_bytes,
            metrics,
        })
    }

    /// The Azure Table row key for this run.
    pub fn row_key(&self) -> String {
        table_key(&self.run_id)
    }
}

#[derive(Deserialize)]
struct RunJson {
    run_id: String,
    status: RunState,
    strategy_id: String,
    #[serde(default, with = "optional_date")]
    data_start: Option<Date>,
    #[serde(default, with = "optional_date")]
    data_end: Option<Date>,
    #[serde(default)]
    run_digest: Option<String>,
    #[serde(default)]
    configuration_digest: Option<String>,
    #[serde(default)]
    artifact_count: u64,
    #[serde(default)]
    total_bytes: u64,
    #[serde(default)]
    metrics: BTreeMap<String, String>,
}

impl Serialize for RunRecord {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("RunRecord", 10)?;
        state.serialize_field("artifact_count", &self.artifact_count)?;
        state.serialize_field("configuration_digest", &self.configuration_digest)?;
        state.serialize_field("data_end", &self.data_end.map(Date::isoformat))?;
        state.serialize_field("data_start", &self.data_start.map(Date::isoformat))?;
        state.serialize_field("metrics", &self.metrics)?;
        state.serialize_field("run_digest", &self.run_digest)?;
        state.serialize_field("run_id", &self.run_id)?;
        state.serialize_field("status", &self.status)?;
        state.serialize_field("strategy_id", &self.strategy_id)?;
        state.serialize_field("total_bytes", &self.total_bytes)?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for RunRecord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = RunJson::deserialize(deserializer)?;
        RunRecord::new(
            raw.run_id,
            raw.status,
            raw.strategy_id,
            raw.data_start,
            raw.data_end,
            raw.run_digest,
            raw.configuration_digest,
            raw.artifact_count,
            raw.total_bytes,
            raw.metrics,
        )
        .map_err(de::Error::custom)
    }
}

/// Point-in-time option contract metadata held by the contracts catalog.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContractRecord {
    /// Canonical OSI option symbol.
    pub symbol: String,
    /// Canonical underlying symbol.
    pub underlying: String,
    /// Expiration date of the contract.
    pub expiration_date: Date,
    /// Strike price at scale 18.
    pub strike_price: Decimal18,
    /// Call or put.
    pub option_type: OptionType,
    /// Deliverable multiplier per contract.
    pub multiplier: u32,
    /// First date the contract traded, if known.
    pub listed_date: Option<Date>,
    /// Last date the contract traded, if known.
    pub delisted_date: Option<Date>,
}

impl ContractRecord {
    /// Build a validated contract record.
    ///
    /// The strike must be positive, the multiplier positive, and the listed
    /// date on or before the delisted date. Mirrors Python's
    /// `ContractRecord.__post_init__`.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        symbol: impl Into<String>,
        underlying: impl Into<String>,
        expiration_date: Date,
        strike_price: Decimal18,
        option_type: OptionType,
        multiplier: u32,
        listed_date: Option<Date>,
        delisted_date: Option<Date>,
    ) -> Result<Self, DataError> {
        if !strike_price.is_positive() {
            return Err(DataError::value("strike_price must be positive"));
        }
        if multiplier == 0 {
            return Err(DataError::value("multiplier must be positive"));
        }
        if let (Some(listed), Some(delisted)) = (listed_date, delisted_date) {
            if listed > delisted {
                return Err(DataError::value(
                    "listed_date must be on or before delisted_date",
                ));
            }
        }
        Ok(Self {
            symbol: canonical_symbol(&symbol.into())?,
            underlying: canonical_symbol(&underlying.into())?,
            expiration_date,
            strike_price,
            option_type,
            multiplier,
            listed_date,
            delisted_date,
        })
    }

    /// Short alias used by option selectors.
    pub fn strike(&self) -> Decimal18 {
        self.strike_price
    }
}

#[derive(Deserialize)]
struct ContractJson {
    symbol: String,
    underlying: String,
    #[serde(with = "date_format")]
    expiration_date: Date,
    strike_price: Decimal18,
    option_type: OptionType,
    #[serde(default = "default_multiplier")]
    multiplier: u32,
    #[serde(default, with = "optional_date")]
    listed_date: Option<Date>,
    #[serde(default, with = "optional_date")]
    delisted_date: Option<Date>,
}

fn default_multiplier() -> u32 {
    100
}

impl Serialize for ContractRecord {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("ContractRecord", 8)?;
        state.serialize_field("delisted_date", &self.delisted_date.map(Date::isoformat))?;
        state.serialize_field("expiration_date", &self.expiration_date.isoformat())?;
        state.serialize_field("listed_date", &self.listed_date.map(Date::isoformat))?;
        state.serialize_field("multiplier", &self.multiplier)?;
        state.serialize_field("option_type", &self.option_type)?;
        state.serialize_field("strike_price", &self.strike_price)?;
        state.serialize_field("symbol", &self.symbol)?;
        state.serialize_field("underlying", &self.underlying)?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for ContractRecord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = ContractJson::deserialize(deserializer)?;
        ContractRecord::new(
            raw.symbol,
            raw.underlying,
            raw.expiration_date,
            raw.strike_price,
            raw.option_type,
            raw.multiplier,
            raw.listed_date,
            raw.delisted_date,
        )
        .map_err(de::Error::custom)
    }
}

/// Point-in-time listing episode for one entity under one symbol.
///
/// `listed_date` is the first session traded under `symbol` and
/// `delisted_date` is the last; both bounds are inclusive. Point-in-time
/// consumers must treat facts dated after an as-of instant as unknown.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListingRecord {
    /// Canonical uppercase entity identifier.
    pub entity_id: String,
    /// Canonical uppercase symbol used by this episode.
    pub symbol: String,
    /// First session traded under the symbol.
    pub listed_date: Date,
    /// Last session traded under the symbol, if the episode ended.
    pub delisted_date: Option<Date>,
    /// How the entity came to be listed under the symbol.
    pub listing_type: ListingType,
    /// Why the episode ended, if it did.
    pub delisting_type: Option<DelistingType>,
    /// Final traded price, when a delisting was recorded.
    pub final_price: Option<Decimal18>,
}

impl ListingRecord {
    /// Build a validated listing episode.
    ///
    /// `delisting_type` and `delisted_date` come in pairs, and a final price
    /// requires a delisting and must be positive. Mirrors Python's
    /// `ListingRecord.__post_init__`.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        entity_id: impl Into<String>,
        symbol: impl Into<String>,
        listed_date: Date,
        delisted_date: Option<Date>,
        listing_type: ListingType,
        delisting_type: Option<DelistingType>,
        final_price: Option<Decimal18>,
    ) -> Result<Self, DataError> {
        let entity_id = entity_id.into().trim().to_uppercase();
        if entity_id.is_empty() {
            return Err(DataError::value("entity_id must not be empty"));
        }
        if let Some(delisted) = delisted_date {
            if listed_date > delisted {
                return Err(DataError::value(
                    "listed_date must be on or before delisted_date",
                ));
            }
        }
        if delisted_date.is_none() && delisting_type.is_some() {
            return Err(DataError::value("delisting_type requires a delisted_date"));
        }
        if delisted_date.is_some() && delisting_type.is_none() {
            return Err(DataError::value("delisted_date requires a delisting_type"));
        }
        if let Some(price) = final_price {
            if delisted_date.is_none() {
                return Err(DataError::value("final_price requires a delisted_date"));
            }
            if !price.is_positive() {
                return Err(DataError::value("final_price must be positive"));
            }
        }
        Ok(Self {
            entity_id,
            symbol: canonical_symbol(&symbol.into())?,
            listed_date,
            delisted_date,
            listing_type,
            delisting_type,
            final_price,
        })
    }

    /// The deterministic logical key for this listing episode.
    pub fn key(&self) -> (String, String) {
        (self.entity_id.clone(), self.symbol.clone())
    }

    /// The Azure Table partition key for this listing episode.
    pub fn partition_key(&self) -> String {
        format!("listings|{}", table_key(&self.entity_id))
    }

    /// The Azure Table row key for this listing episode.
    pub fn row_key(&self) -> String {
        table_key(&self.symbol)
    }

    /// Whether the listing itself was known on `as_of`.
    pub fn is_known(&self, as_of: Date) -> bool {
        self.listed_date <= as_of
    }

    /// Point-in-time activity of this episode on `day`.
    ///
    /// The listing must be known on `as_of` and cover `day`. A delisting dated
    /// after `as_of` is not yet known, so the episode stays active; once the
    /// delisting date has arrived, the episode covers only sessions on or
    /// before it.
    pub fn active_on(&self, day: Date, as_of: Date) -> bool {
        if self.listed_date > as_of || day < self.listed_date {
            return false;
        }
        match self.delisted_date {
            None => true,
            Some(delisted) if delisted > as_of => true,
            Some(delisted) => day <= delisted,
        }
    }
}

#[derive(Deserialize)]
struct ListingJson {
    entity_id: String,
    symbol: String,
    #[serde(with = "date_format")]
    listed_date: Date,
    #[serde(default, with = "optional_date")]
    delisted_date: Option<Date>,
    #[serde(default)]
    listing_type: Option<ListingType>,
    #[serde(default)]
    delisting_type: Option<DelistingType>,
    #[serde(default)]
    final_price: Option<Decimal18>,
}

impl Serialize for ListingRecord {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("ListingRecord", 7)?;
        state.serialize_field("delisted_date", &self.delisted_date.map(Date::isoformat))?;
        state.serialize_field("delisting_type", &self.delisting_type)?;
        state.serialize_field("entity_id", &self.entity_id)?;
        state.serialize_field("final_price", &self.final_price)?;
        state.serialize_field("listed_date", &self.listed_date.isoformat())?;
        state.serialize_field("listing_type", &self.listing_type)?;
        state.serialize_field("symbol", &self.symbol)?;
        state.end()
    }
}

impl<'de> Deserialize<'de> for ListingRecord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let raw = ListingJson::deserialize(deserializer)?;
        ListingRecord::new(
            raw.entity_id,
            raw.symbol,
            raw.listed_date,
            raw.delisted_date,
            raw.listing_type.unwrap_or(ListingType::Unknown),
            raw.delisting_type,
            raw.final_price,
        )
        .map_err(de::Error::custom)
    }
}

/// Canonicalise an entity identifier the way Python's listing code does.
pub(crate) fn canonical_entity_id(value: &str) -> Result<String, DataError> {
    let canonical = value.trim().to_uppercase();
    if canonical.is_empty() {
        return Err(DataError::value("entity_id must not be empty"));
    }
    Ok(canonical)
}

/// Canonicalise a front-set underlying the way Python's front sets do.
pub(crate) fn canonical_underlying(value: &str) -> Result<String, DataError> {
    let canonical = value.trim().to_uppercase();
    if canonical.is_empty() {
        return Err(DataError::value("underlying must not be empty"));
    }
    Ok(canonical)
}

#[cfg(test)]
#[path = "../tests/unit/models.rs"]
mod tests;
