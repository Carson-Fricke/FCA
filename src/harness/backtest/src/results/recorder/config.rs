//! Recorder configuration, record-kind enums, and the clock seam.

use std::collections::BTreeMap;
use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;

/// A deterministic time source in epoch nanoseconds since the Unix epoch.
///
/// The Python recorder accepts a `harness.core.clock.Clock`. The Rust port
/// keeps the same injection point: the kernel never reads the wall clock, and
/// callers either pass an explicit timestamp or an implementation of this
/// trait.
pub trait Clock: Send + Sync {
    /// Return the current time as nanoseconds since the Unix epoch.
    fn now_ns(&self) -> i64;
}

/// A fixed clock useful for deterministic tests and fakes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FixedClock {
    /// The nanosecond timestamp the clock always reports.
    pub timestamp_ns: i64,
}

impl FixedClock {
    /// Create a fixed clock at `timestamp_ns`.
    #[must_use]
    pub const fn new(timestamp_ns: i64) -> Self {
        Self { timestamp_ns }
    }
}

impl Clock for FixedClock {
    fn now_ns(&self) -> i64 {
        self.timestamp_ns
    }
}

/// A deterministic engine identifier seed.
///
/// Mirrors the Python `str | int | bytes` seed accepted by `RecorderConfig`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Seed {
    /// A textual seed.
    Text(String),
    /// An integer seed.
    Int(i64),
    /// A byte seed, encoded as lowercase hex in the manifest.
    Bytes(Vec<u8>),
}

impl Default for Seed {
    fn default() -> Self {
        Self::Text("harness-run-recorder".to_string())
    }
}

impl Seed {
    /// Render the seed exactly like `_seed_text`: hex for bytes, `str` otherwise.
    #[must_use]
    pub fn as_text(&self) -> String {
        match self {
            Self::Text(value) => value.clone(),
            Self::Int(value) => value.to_string(),
            Self::Bytes(value) => crate::sha256::hex(value),
        }
    }
}

impl From<&str> for Seed {
    fn from(value: &str) -> Self {
        Self::Text(value.to_string())
    }
}

impl From<String> for Seed {
    fn from(value: String) -> Self {
        Self::Text(value)
    }
}

impl From<i64> for Seed {
    fn from(value: i64) -> Self {
        Self::Int(value)
    }
}

/// Why a strategy produced one recorded intent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActionKind {
    /// Submit an order.
    Submit,
    /// Cancel an order.
    Cancel,
    /// Replace an order.
    Replace,
    /// Hold with no order.
    Hold,
    /// A strategy-defined custom action.
    Custom,
}

impl ActionKind {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Submit => "submit",
            Self::Cancel => "cancel",
            Self::Replace => "replace",
            Self::Hold => "hold",
            Self::Custom => "custom",
        }
    }

    /// Parse an action kind from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "submit" => Some(Self::Submit),
            "cancel" => Some(Self::Cancel),
            "replace" => Some(Self::Replace),
            "hold" => Some(Self::Hold),
            "custom" => Some(Self::Custom),
            _ => None,
        }
    }
}

/// What moved a position from one state to the next.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransitionReason {
    /// A fill changed the position.
    Fill,
    /// A mark-to-market changed the position valuation.
    Mark,
    /// A periodic snapshot.
    Snapshot,
    /// A position close.
    Close,
}

impl TransitionReason {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Fill => "fill",
            Self::Mark => "mark",
            Self::Snapshot => "snapshot",
            Self::Close => "close",
        }
    }

    /// Parse a transition reason from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "fill" => Some(Self::Fill),
            "mark" => Some(Self::Mark),
            "snapshot" => Some(Self::Snapshot),
            "close" => Some(Self::Close),
            _ => None,
        }
    }
}

/// Kind of recorded state marker.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MarkerScope {
    /// A run-level marker.
    Run,
    /// A strategy-state marker.
    State,
    /// A position mark.
    Position,
    /// A free-form note.
    Note,
}

impl MarkerScope {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Run => "run",
            Self::State => "state",
            Self::Position => "position",
            Self::Note => "note",
        }
    }

    /// Parse a marker scope from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "run" => Some(Self::Run),
            "state" => Some(Self::State),
            "position" => Some(Self::Position),
            "note" => Some(Self::Note),
            _ => None,
        }
    }
}

/// A recorder configuration validation failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// A required string was empty or whitespace.
    EmptyString(&'static str),
    /// `batch_size` was zero.
    BatchSize,
    /// A required mapping key was invalid.
    MetadataKey(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyString(name) => write!(formatter, "{name} must be a non-empty string"),
            Self::BatchSize => formatter.write_str("batch_size must be a positive integer"),
            Self::MetadataKey(key) => {
                write!(formatter, "metadata key {key:?} must be a non-empty string")
            }
        }
    }
}

impl std::error::Error for ConfigError {}

/// Trimmed non-empty text, mirroring `harness.core.types.common.text`.
///
/// # Errors
///
/// Returns [`ConfigError::EmptyString`] when `value` is empty or whitespace.
pub fn text(value: &str, field_name: &'static str) -> Result<String, ConfigError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(ConfigError::EmptyString(field_name));
    }
    Ok(trimmed.to_string())
}

/// Immutable configuration for one recorded run.
///
/// Mirrors `harness.results.recorder.config.RecorderConfig`. The struct is
/// built through [`RecorderConfig::new`] so every validation rule the Python
/// `__post_init__` enforces is applied.
#[derive(Clone)]
pub struct RecorderConfig {
    /// Run identifier.
    pub run_id: String,
    /// Directory holding the run artifacts.
    pub directory: PathBuf,
    /// Deterministic identifier seed.
    pub seed: Seed,
    /// Rows buffered per artifact before a flush.
    pub batch_size: usize,
    /// Whether the optional full event stream is recorded.
    pub record_events: bool,
    /// Whether event payloads are serialized.
    pub record_payloads: bool,
    /// Artifact compression codec.
    pub compression: String,
    /// Parquet writer version.
    pub parquet_version: String,
    /// Optional deterministic clock.
    pub clock: Option<Arc<dyn Clock>>,
    /// Default strategy identifier.
    pub strategy_id: Option<String>,
    /// Default book identifier.
    pub book_id: Option<String>,
    /// Default account identifier.
    pub account_id: Option<String>,
    /// Free-form run metadata.
    pub metadata: BTreeMap<String, String>,
}

impl fmt::Debug for RecorderConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RecorderConfig")
            .field("run_id", &self.run_id)
            .field("directory", &self.directory)
            .field("seed", &self.seed)
            .field("batch_size", &self.batch_size)
            .field("record_events", &self.record_events)
            .field("record_payloads", &self.record_payloads)
            .field("compression", &self.compression)
            .field("parquet_version", &self.parquet_version)
            .field("clock", &self.clock.as_ref().map(|_| "Clock"))
            .field("strategy_id", &self.strategy_id)
            .field("book_id", &self.book_id)
            .field("account_id", &self.account_id)
            .field("metadata", &self.metadata)
            .finish()
    }
}

impl RecorderConfig {
    /// Build a validated recorder configuration.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when `run_id` is blank, `batch_size` is zero,
    /// `compression` or `parquet_version` is blank, or a metadata key is blank.
    pub fn new(
        run_id: impl Into<String>,
        directory: impl Into<PathBuf>,
    ) -> Result<Self, ConfigError> {
        let run_id = text(&run_id.into(), "run_id")?;
        Ok(Self {
            run_id,
            directory: directory.into(),
            seed: Seed::default(),
            batch_size: 4096,
            record_events: false,
            record_payloads: true,
            compression: "zstd".to_string(),
            parquet_version: "2.6".to_string(),
            clock: None,
            strategy_id: None,
            book_id: None,
            account_id: None,
            metadata: BTreeMap::new(),
        })
    }

    /// Set the deterministic seed.
    #[must_use]
    pub fn with_seed(mut self, seed: impl Into<Seed>) -> Self {
        self.seed = seed.into();
        self
    }

    /// Set the per-artifact batch size.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::BatchSize`] when `batch_size` is zero.
    pub fn with_batch_size(mut self, batch_size: usize) -> Result<Self, ConfigError> {
        if batch_size < 1 {
            return Err(ConfigError::BatchSize);
        }
        self.batch_size = batch_size;
        Ok(self)
    }

    /// Enable or disable the optional full event stream.
    #[must_use]
    pub fn with_record_events(mut self, record_events: bool) -> Self {
        self.record_events = record_events;
        self
    }

    /// Enable or disable event payloads.
    #[must_use]
    pub fn with_record_payloads(mut self, record_payloads: bool) -> Self {
        self.record_payloads = record_payloads;
        self
    }

    /// Set the artifact compression codec.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::EmptyString`] when the codec is blank.
    pub fn with_compression(mut self, compression: &str) -> Result<Self, ConfigError> {
        self.compression = text(compression, "compression")?;
        Ok(self)
    }

    /// Set the Parquet writer version.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::EmptyString`] when the version is blank.
    pub fn with_parquet_version(mut self, version: &str) -> Result<Self, ConfigError> {
        self.parquet_version = text(version, "parquet_version")?;
        Ok(self)
    }

    /// Inject a deterministic clock.
    #[must_use]
    pub fn with_clock(mut self, clock: Arc<dyn Clock>) -> Self {
        self.clock = Some(clock);
        self
    }

    /// Set the default strategy identifier.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::EmptyString`] when the identifier is blank.
    pub fn with_strategy_id(mut self, strategy_id: &str) -> Result<Self, ConfigError> {
        self.strategy_id = Some(text(strategy_id, "strategy_id")?);
        Ok(self)
    }

    /// Set the default book identifier.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::EmptyString`] when the identifier is blank.
    pub fn with_book_id(mut self, book_id: &str) -> Result<Self, ConfigError> {
        self.book_id = Some(text(book_id, "book_id")?);
        Ok(self)
    }

    /// Set the default account identifier.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::EmptyString`] when the identifier is blank.
    pub fn with_account_id(mut self, account_id: &str) -> Result<Self, ConfigError> {
        self.account_id = Some(text(account_id, "account_id")?);
        Ok(self)
    }

    /// Insert one metadata entry.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::MetadataKey`] when the key is blank.
    pub fn with_metadata(
        mut self,
        key: &str,
        value: impl Into<String>,
    ) -> Result<Self, ConfigError> {
        let key =
            text(key, "metadata key").map_err(|_| ConfigError::MetadataKey(key.to_string()))?;
        self.metadata.insert(key, value.into());
        Ok(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python() {
        let config = RecorderConfig::new("run", "out").expect("config");
        assert_eq!(config.seed, Seed::Text("harness-run-recorder".to_string()));
        assert_eq!(config.batch_size, 4096);
        assert!(!config.record_events);
        assert!(config.record_payloads);
        assert_eq!(config.compression, "zstd");
        assert_eq!(config.parquet_version, "2.6");
    }

    #[test]
    fn blank_run_id_is_rejected() {
        assert_eq!(
            RecorderConfig::new("   ", "out").err(),
            Some(ConfigError::EmptyString("run_id"))
        );
    }

    #[test]
    fn zero_batch_size_is_rejected() {
        let config = RecorderConfig::new("run", "out").expect("config");
        assert_eq!(
            config.with_batch_size(0).err(),
            Some(ConfigError::BatchSize)
        );
    }

    #[test]
    fn seed_text_encoding() {
        assert_eq!(Seed::Int(-7).as_text(), "-7");
        assert_eq!(Seed::Bytes(vec![0x00, 0xab, 0xff]).as_text(), "00abff");
        assert_eq!(Seed::Text("x".to_string()).as_text(), "x");
    }

    #[test]
    fn enum_values_round_trip() {
        for kind in [
            ActionKind::Submit,
            ActionKind::Cancel,
            ActionKind::Replace,
            ActionKind::Hold,
            ActionKind::Custom,
        ] {
            assert_eq!(ActionKind::parse(kind.value()), Some(kind));
        }
        for reason in [
            TransitionReason::Fill,
            TransitionReason::Mark,
            TransitionReason::Snapshot,
            TransitionReason::Close,
        ] {
            assert_eq!(TransitionReason::parse(reason.value()), Some(reason));
        }
        for scope in [
            MarkerScope::Run,
            MarkerScope::State,
            MarkerScope::Position,
            MarkerScope::Note,
        ] {
            assert_eq!(MarkerScope::parse(scope.value()), Some(scope));
        }
    }

    #[test]
    fn fixed_clock_reports_its_timestamp() {
        let clock = FixedClock::new(42);
        assert_eq!(clock.now_ns(), 42);
    }
}
