//! The streaming [`RunRecorder`].
//!
//! Port of `harness.results.recorder.recorder.RunRecorder`. A recorder turns one
//! deterministic run into referentially linked artifacts. Every artifact is
//! written through a bounded row batch, so a run of any length keeps at most
//! `batch_size` pending rows per artifact in memory. Nothing here reads wall
//! time, opens a network connection, or uses unseeded randomness: timestamps
//! come from the caller or an injected [`Clock`], and every generated
//! identifier comes from [`EngineIdSource`].

use std::collections::BTreeMap;
use std::path::PathBuf;

use harness_contracts::canonical_json::JsonValue;

use super::artifacts::artifact_path;
use super::columns::Cell;
use super::config::text;
use super::config::ActionKind;
use super::config::MarkerScope;
use super::config::RecorderConfig;
use super::config::TransitionReason;
use super::ids::EngineIdSource;
use super::ids::IdError;
use super::manifest::file_digest;
use super::manifest::manifest_rows;
use super::manifest::run_fingerprint;
use super::manifest::ManifestRowsInput;
use super::manifest::RunManifest;
use super::parquet::ParquetArtifactWriter;
use super::schemas::Artifact;
use super::schemas::STREAM_ARTIFACTS;
use super::writer::build_row;
use super::writer::Row;
use super::writer::RowSink;
use super::writer::Value;
use super::writer::WriterError;

/// Exact numeric portfolio sample. Decimal text is encoded once at the sink;
/// equal consecutive values reuse their canonical formatting.
pub struct PortfolioEquityRecord {
    pub timestamp_ns: i64,
    pub cash: num::Decimal18,
    pub equity: num::Decimal18,
    pub market_value: num::Decimal18,
    pub realized_pnl: num::Decimal18,
    pub unrealized_pnl: num::Decimal18,
    pub total_pnl: num::Decimal18,
    pub position_count: u32,
    pub fill_count: u64,
}

/// Recorder construction or streaming failure.
#[derive(Debug)]
pub enum RecorderError {
    /// A writer failed.
    Writer(WriterError),
    /// Identifier derivation failed.
    Id(IdError),
    /// The recorder was used after `close`.
    Closed,
    /// A timestamp was required but neither supplied nor clock-configured.
    TimestampRequired,
    /// An account identifier was required but not supplied or configured.
    AccountRequired,
    /// A configured text field was blank.
    Config(super::config::ConfigError),
    /// The manifest was requested before `close`.
    ManifestUnavailable,
}

impl std::fmt::Display for RecorderError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Writer(error) => write!(formatter, "{error}"),
            Self::Id(error) => write!(formatter, "{error}"),
            Self::Closed => formatter.write_str("the recorder is closed"),
            Self::TimestampRequired => formatter
                .write_str("timestamp is required when the recorder has no configured clock"),
            Self::AccountRequired => formatter.write_str("account_id is required for this record"),
            Self::Config(error) => write!(formatter, "{error}"),
            Self::ManifestUnavailable => {
                formatter.write_str("the run manifest is only available after close()")
            }
        }
    }
}

impl std::error::Error for RecorderError {}

impl From<WriterError> for RecorderError {
    fn from(error: WriterError) -> Self {
        Self::Writer(error)
    }
}

impl From<IdError> for RecorderError {
    fn from(error: IdError) -> Self {
        Self::Id(error)
    }
}

impl From<super::config::ConfigError> for RecorderError {
    fn from(error: super::config::ConfigError) -> Self {
        Self::Config(error)
    }
}

/// One strategy intent to record.
#[derive(Clone, Debug, Default)]
pub struct ActionRecord {
    /// Why the strategy produced the intent.
    pub kind: Option<ActionKind>,
    /// Strategy identifier override.
    pub strategy_id: Option<String>,
    /// Book identifier override.
    pub book_id: Option<String>,
    /// Symbol, if any.
    pub symbol: Option<String>,
    /// Side, if any.
    pub side: Option<String>,
    /// String-exact quantity, if any.
    pub quantity: Option<String>,
    /// Related order identifier, if any.
    pub order_id: Option<String>,
    /// Non-null reason, if any.
    pub reason: Option<String>,
    /// Free-form JSON detail.
    pub detail: Option<JsonValue>,
    /// Explicit timestamp in epoch nanoseconds.
    pub at_ns: Option<i64>,
}

/// One fill to record.
#[derive(Clone, Debug)]
pub struct FillRecord {
    /// Deterministic fill identifier.
    pub fill_id: String,
    /// Fill sequence.
    pub sequence: u64,
    /// Fill timestamp in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Originating event identifier.
    pub event_id: String,
    /// Originating order identifier.
    pub order_id: String,
    /// Linked action identifier.
    pub action_id: Option<String>,
    /// Linked risk decision identifier.
    pub risk_decision_id: Option<String>,
    /// Linked position transition identifier.
    pub position_transition_id: Option<String>,
    /// Linked equity snapshot identifier.
    pub equity_snapshot_id: Option<String>,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: String,
    /// String-exact filled quantity.
    pub quantity: String,
    /// String-exact fill price.
    pub price: String,
    /// String-exact fee.
    pub fee: String,
    /// Liquidity flag, if any.
    pub liquidity: Option<String>,
}

/// One OMS execution report to record.
#[derive(Clone, Debug)]
pub struct ExecutionReportRecord {
    /// Originating event identifier.
    pub event_id: String,
    /// Report timestamp in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Report sequence.
    pub sequence: u64,
    /// Deterministic report identifier.
    pub report_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Report status text.
    pub status: String,
    /// Execution type text.
    pub execution_type: String,
    /// String-exact order quantity.
    pub order_quantity: String,
    /// String-exact filled quantity.
    pub filled_quantity: String,
    /// String-exact remaining quantity.
    pub remaining_quantity: String,
    /// String-exact average fill price, if any.
    pub average_fill_price: Option<String>,
    /// String-exact last fill quantity.
    pub last_fill_quantity: String,
    /// String-exact last fill price, if any.
    pub last_fill_price: Option<String>,
    /// Optional free text.
    pub text: Option<String>,
}

/// One position transition to record.
#[derive(Clone, Debug)]
pub struct PositionTransitionRecord {
    /// Transition reason.
    pub reason: Option<TransitionReason>,
    /// Account identifier override.
    pub account_id: Option<String>,
    /// Book identifier override.
    pub book_id: Option<String>,
    /// Instrument symbol.
    pub symbol: String,
    /// Linked fill identifier.
    pub fill_id: Option<String>,
    /// String-exact quantity before the transition.
    pub quantity_before: String,
    /// String-exact quantity after the transition.
    pub quantity_after: String,
    /// String-exact cost basis.
    pub cost_basis: String,
    /// String-exact cash flow.
    pub cash_flow: String,
    /// String-exact realized PnL.
    pub realized_pnl: String,
    /// String-exact mark price, if any.
    pub mark_price: Option<String>,
    /// String-exact market value, if any.
    pub market_value: Option<String>,
    /// String-exact unrealized PnL, if any.
    pub unrealized_pnl: Option<String>,
    /// Explicit timestamp in epoch nanoseconds.
    pub at_ns: Option<i64>,
    /// Pre-reserved transition identifier.
    pub transition_id: Option<String>,
}

/// One book, portfolio, or account equity snapshot to record.
#[derive(Clone, Debug)]
pub struct EquityRecord {
    /// Snapshot source (`book`, `portfolio`, or `account`).
    pub source: String,
    /// Account identifier override.
    pub account_id: Option<String>,
    /// Book identifier override.
    pub book_id: Option<String>,
    /// String-exact cash.
    pub cash: String,
    /// String-exact equity, if any.
    pub equity: Option<String>,
    /// String-exact market value, if any.
    pub market_value: Option<String>,
    /// String-exact realized PnL.
    pub realized_pnl: String,
    /// String-exact unrealized PnL, if any.
    pub unrealized_pnl: Option<String>,
    /// String-exact total PnL, if any.
    pub total_pnl: Option<String>,
    /// Open position count.
    pub position_count: u32,
    /// Fill count.
    pub fill_count: u64,
    /// Explicit timestamp in epoch nanoseconds.
    pub at_ns: Option<i64>,
    /// Pre-reserved snapshot identifier.
    pub snapshot_id: Option<String>,
}

/// One event to record in the optional full event stream.
#[derive(Clone, Debug)]
pub struct EventRecord {
    /// Event identifier.
    pub event_id: String,
    /// Event sequence.
    pub sequence: u64,
    /// Event timestamp in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Event type name.
    pub event_type: String,
    /// Fully qualified Python type name.
    pub type_name: String,
    /// Symbol, if any.
    pub symbol: Option<String>,
    /// Canonical JSON payload, if any.
    pub payload: Option<JsonValue>,
}

/// Stream one deterministic run into linked, bounded-memory artifacts.
pub struct RunRecorder {
    config: RecorderConfig,
    ids: EngineIdSource,
    sequence: u64,
    closed: bool,
    manifest: Option<RunManifest>,
    writers: BTreeMap<Artifact, ParquetArtifactWriter>,
}

impl RunRecorder {
    /// Create a recorder and its per-artifact writers.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] when the seed is empty or the directory cannot
    /// be created.
    pub fn new(config: RecorderConfig) -> Result<Self, RecorderError> {
        let ids = EngineIdSource::new(config.seed.clone())?;
        std::fs::create_dir_all(&config.directory)
            .map_err(|error| RecorderError::Writer(WriterError::Io(error)))?;
        let directory = config.directory.clone();
        let batch_size = config.batch_size;
        let mut writers = BTreeMap::new();
        for artifact in STREAM_ARTIFACTS {
            writers.insert(
                artifact,
                ParquetArtifactWriter::new(
                    artifact_path(&directory, artifact),
                    artifact,
                    batch_size,
                ),
            );
        }
        Ok(Self {
            config,
            ids,
            sequence: 0,
            closed: false,
            manifest: None,
            writers,
        })
    }

    /// The recorder configuration.
    #[must_use]
    pub const fn config(&self) -> &RecorderConfig {
        &self.config
    }

    /// Whether the run has been finalized.
    #[must_use]
    pub const fn closed(&self) -> bool {
        self.closed
    }

    /// The run manifest; only available after [`RunRecorder::close`].
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError::ManifestUnavailable`] before `close`.
    pub fn manifest(&self) -> Result<&RunManifest, RecorderError> {
        self.manifest
            .as_ref()
            .ok_or(RecorderError::ManifestUnavailable)
    }

    /// Rows flushed so far per artifact name.
    #[must_use]
    pub fn row_counts(&self) -> BTreeMap<String, u64> {
        self.writers
            .iter()
            .map(|(artifact, writer)| {
                (
                    artifact.value().to_string(),
                    writer.row_count() + writer.pending_rows() as u64,
                )
            })
            .collect()
    }

    /// Largest pending row count across every artifact.
    #[must_use]
    pub fn buffered_rows(&self) -> usize {
        self.writers
            .values()
            .map(RowSink::pending_rows)
            .max()
            .unwrap_or(0)
    }

    /// Issue the next deterministic identifier for forward linking.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError::Id`] when `kind` is empty.
    pub fn reserve_id(&mut self, kind: &str) -> Result<String, RecorderError> {
        Ok(self.ids.next_id(kind)?)
    }

    /// Append one pre-built schema row for `artifact`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError::Closed`] after `close`, or a [`WriterError`]
    /// when the row does not match the artifact schema.
    pub fn write(&mut self, artifact: Artifact, row: &Row) -> Result<(), RecorderError> {
        if self.closed {
            return Err(RecorderError::Closed);
        }
        let Some(writer) = self.writers.get_mut(&artifact) else {
            return Err(RecorderError::Writer(WriterError::UnknownField {
                artifact,
                field: "<generic>",
            }));
        };
        writer.append(row).map_err(RecorderError::Writer)
    }

    /// Build and append a schema row from named cells.
    ///
    /// # Errors
    ///
    /// Returns a [`WriterError`] for schema violations or
    /// [`RecorderError::Closed`] after `close`.
    pub fn record_cells(
        &mut self,
        artifact: Artifact,
        cells: impl IntoIterator<Item = (&'static str, Value)>,
    ) -> Result<(), RecorderError> {
        let row = build_row(artifact, cells)?;
        self.write(artifact, &row)
    }

    /// Record one strategy intent and return its `action_id`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on timestamp, identifier, or write failure.
    pub fn record_action(&mut self, action: &ActionRecord) -> Result<String, RecorderError> {
        let action_id = self.reserve_id("action")?;
        let kind = action.kind.unwrap_or(ActionKind::Custom);
        let timestamp = self.resolve_timestamp(action.at_ns)?;
        let sequence = self.next_sequence();
        let strategy = self.strategy_id(action.strategy_id.as_deref())?;
        let book = self.book_id(action.book_id.as_deref())?;
        self.record_cells(
            Artifact::Actions,
            [
                ("action_id", Value::utf8(action_id.clone())),
                ("sequence", Value::UInt64(sequence)),
                ("timestamp", Value::TimestampNs(timestamp)),
                ("strategy_id", Value::utf8(strategy)),
                ("book_id", Value::optional_utf8(book)),
                ("kind", Value::utf8(kind.value())),
                ("symbol", Value::optional_utf8(action.symbol.clone())),
                ("side", Value::optional_utf8(action.side.clone())),
                ("quantity", Value::optional_utf8(action.quantity.clone())),
                ("order_id", Value::optional_utf8(action.order_id.clone())),
                ("reason", Value::optional_utf8(action.reason.clone())),
                (
                    "detail",
                    Value::optional_utf8(
                        action.detail.as_ref().map(JsonValue::to_canonical_string),
                    ),
                ),
            ],
        )?;
        Ok(action_id)
    }

    /// Record one fill with every link known at recording time.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on write failure.
    pub fn record_fill(&mut self, fill: &FillRecord) -> Result<String, RecorderError> {
        self.record_cells(
            Artifact::Fills,
            [
                ("fill_id", Value::utf8(fill.fill_id.clone())),
                ("sequence", Value::UInt64(fill.sequence)),
                ("timestamp", Value::TimestampNs(fill.timestamp_ns)),
                ("event_id", Value::utf8(fill.event_id.clone())),
                ("order_id", Value::utf8(fill.order_id.clone())),
                ("action_id", Value::optional_utf8(fill.action_id.clone())),
                (
                    "risk_decision_id",
                    Value::optional_utf8(fill.risk_decision_id.clone()),
                ),
                (
                    "position_transition_id",
                    Value::optional_utf8(fill.position_transition_id.clone()),
                ),
                (
                    "equity_snapshot_id",
                    Value::optional_utf8(fill.equity_snapshot_id.clone()),
                ),
                ("symbol", Value::utf8(fill.symbol.clone())),
                ("side", Value::utf8(fill.side.clone())),
                ("quantity", Value::utf8(fill.quantity.clone())),
                ("price", Value::utf8(fill.price.clone())),
                ("fee", Value::utf8(fill.fee.clone())),
                ("liquidity", Value::optional_utf8(fill.liquidity.clone())),
            ],
        )?;
        Ok(fill.fill_id.clone())
    }

    /// Record one OMS execution report and return its `report_id`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on write failure.
    pub fn record_execution_report(
        &mut self,
        report: &ExecutionReportRecord,
    ) -> Result<String, RecorderError> {
        self.record_cells(
            Artifact::ExecutionReports,
            [
                ("event_id", Value::utf8(report.event_id.clone())),
                ("timestamp", Value::TimestampNs(report.timestamp_ns)),
                ("sequence", Value::UInt64(report.sequence)),
                ("report_id", Value::utf8(report.report_id.clone())),
                ("order_id", Value::utf8(report.order_id.clone())),
                ("status", Value::utf8(report.status.clone())),
                ("execution_type", Value::utf8(report.execution_type.clone())),
                ("order_quantity", Value::utf8(report.order_quantity.clone())),
                (
                    "filled_quantity",
                    Value::utf8(report.filled_quantity.clone()),
                ),
                (
                    "remaining_quantity",
                    Value::utf8(report.remaining_quantity.clone()),
                ),
                (
                    "average_fill_price",
                    Value::optional_utf8(report.average_fill_price.clone()),
                ),
                (
                    "last_fill_quantity",
                    Value::utf8(report.last_fill_quantity.clone()),
                ),
                (
                    "last_fill_price",
                    Value::optional_utf8(report.last_fill_price.clone()),
                ),
                ("text", Value::optional_utf8(report.text.clone())),
            ],
        )?;
        Ok(report.report_id.clone())
    }

    /// Record one position transition and return its `transition_id`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on timestamp, identifier, or write failure.
    pub fn record_position_transition(
        &mut self,
        transition: &PositionTransitionRecord,
    ) -> Result<String, RecorderError> {
        let transition_id = match &transition.transition_id {
            Some(value) => text(value, "transition_id")?,
            None => self.reserve_id("position")?,
        };
        let reason = transition.reason.unwrap_or(TransitionReason::Fill);
        let timestamp = self.resolve_timestamp(transition.at_ns)?;
        let sequence = self.next_sequence();
        let account = self.account_id(transition.account_id.as_deref())?;
        let book = self.book_id(transition.book_id.as_deref())?;
        self.record_cells(
            Artifact::PositionTransitions,
            [
                ("transition_id", Value::utf8(transition_id.clone())),
                ("sequence", Value::UInt64(sequence)),
                ("timestamp", Value::TimestampNs(timestamp)),
                ("account_id", Value::utf8(account)),
                ("book_id", Value::optional_utf8(book)),
                ("symbol", Value::utf8(transition.symbol.clone())),
                ("reason", Value::utf8(reason.value())),
                ("fill_id", Value::optional_utf8(transition.fill_id.clone())),
                (
                    "quantity_before",
                    Value::utf8(transition.quantity_before.clone()),
                ),
                (
                    "quantity_after",
                    Value::utf8(transition.quantity_after.clone()),
                ),
                ("cost_basis", Value::utf8(transition.cost_basis.clone())),
                ("cash_flow", Value::utf8(transition.cash_flow.clone())),
                ("realized_pnl", Value::utf8(transition.realized_pnl.clone())),
                (
                    "mark_price",
                    Value::optional_utf8(transition.mark_price.clone()),
                ),
                (
                    "market_value",
                    Value::optional_utf8(transition.market_value.clone()),
                ),
                (
                    "unrealized_pnl",
                    Value::optional_utf8(transition.unrealized_pnl.clone()),
                ),
            ],
        )?;
        Ok(transition_id)
    }

    /// Record one equity snapshot and return its `snapshot_id`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on timestamp, identifier, or write failure.
    pub fn record_equity(&mut self, equity: &EquityRecord) -> Result<String, RecorderError> {
        if self.closed {
            return Err(RecorderError::Closed);
        }
        let snapshot_id = match &equity.snapshot_id {
            Some(value) => text(value, "snapshot_id")?,
            None => self.reserve_id("equity")?,
        };
        let timestamp = self.resolve_timestamp(equity.at_ns)?;
        let sequence = self.next_sequence();
        let account = self.account_id(equity.account_id.as_deref())?;
        let book = self.book_id(equity.book_id.as_deref())?;
        self.writers
            .get_mut(&Artifact::Equity)
            .expect("equity writer")
            .append_cells([
                Cell::Text(Some(&snapshot_id)),
                Cell::U64(sequence),
                Cell::Time(timestamp),
                Cell::Text(Some(&account)),
                Cell::Text(book.as_deref()),
                Cell::Text(Some(&equity.source)),
                Cell::Text(Some(&equity.cash)),
                Cell::Text(equity.equity.as_deref()),
                Cell::Text(equity.market_value.as_deref()),
                Cell::Text(Some(&equity.realized_pnl)),
                Cell::Text(equity.unrealized_pnl.as_deref()),
                Cell::Text(equity.total_pnl.as_deref()),
                Cell::U32(equity.position_count),
                Cell::U64(equity.fill_count),
            ])?;
        Ok(snapshot_id)
    }

    /// Encode a full-resolution numeric portfolio sample directly into Arrow columns.
    pub fn record_portfolio_equity(
        &mut self,
        equity: &PortfolioEquityRecord,
    ) -> Result<(), RecorderError> {
        if self.closed {
            return Err(RecorderError::Closed);
        }
        let snapshot_id = self.reserve_id("equity")?;
        let sequence = self.next_sequence();
        let account = self
            .config
            .account_id
            .as_deref()
            .ok_or(RecorderError::AccountRequired)?;
        self.writers
            .get_mut(&Artifact::Equity)
            .expect("equity writer")
            .append_cells([
                Cell::Text(Some(&snapshot_id)),
                Cell::U64(sequence),
                Cell::Time(equity.timestamp_ns),
                Cell::Text(Some(account)),
                Cell::Text(self.config.book_id.as_deref()),
                Cell::Text(Some("portfolio")),
                Cell::Decimal(equity.cash),
                Cell::Decimal(equity.equity),
                Cell::Decimal(equity.market_value),
                Cell::Decimal(equity.realized_pnl),
                Cell::Decimal(equity.unrealized_pnl),
                Cell::Decimal(equity.total_pnl),
                Cell::U32(equity.position_count),
                Cell::U64(equity.fill_count),
            ])?;
        Ok(())
    }

    /// Record one structured emission and return its `emission_id`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on identifier or write failure.
    pub fn record_emission(
        &mut self,
        name: &str,
        fields: &JsonValue,
        strategy_id: Option<&str>,
        book_id: Option<&str>,
        at_ns: Option<i64>,
    ) -> Result<String, RecorderError> {
        let emission_id = self.reserve_id("emission")?;
        let sequence = self.next_sequence();
        let timestamp = self.resolve_timestamp(at_ns)?;
        let strategy = self.strategy_id(strategy_id)?;
        let book = self.book_id(book_id)?;
        let name = text(name, "name")?;
        self.record_cells(
            Artifact::Emissions,
            [
                ("emission_id", Value::utf8(emission_id.clone())),
                ("sequence", Value::UInt64(sequence)),
                ("timestamp", Value::TimestampNs(timestamp)),
                ("strategy_id", Value::utf8(strategy)),
                ("book_id", Value::optional_utf8(book)),
                ("name", Value::utf8(name)),
                ("fields", Value::utf8(fields.to_canonical_string())),
            ],
        )?;
        Ok(emission_id)
    }

    /// Record one marker and return its `marker_id`.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on identifier or write failure.
    pub fn record_marker(
        &mut self,
        key: &str,
        value: Option<&str>,
        scope: MarkerScope,
        strategy_id: Option<&str>,
        detail: Option<&JsonValue>,
        at_ns: Option<i64>,
    ) -> Result<String, RecorderError> {
        let marker_id = self.reserve_id("marker")?;
        let sequence = self.next_sequence();
        let timestamp = self.resolve_timestamp(at_ns)?;
        let strategy = self.strategy_id(strategy_id)?;
        let key = text(key, "key")?;
        self.record_cells(
            Artifact::Markers,
            [
                ("marker_id", Value::utf8(marker_id.clone())),
                ("sequence", Value::UInt64(sequence)),
                ("timestamp", Value::TimestampNs(timestamp)),
                ("strategy_id", Value::utf8(strategy)),
                ("scope", Value::utf8(scope.value())),
                ("key", Value::utf8(key)),
                ("value", Value::optional_utf8(value.map(str::to_string))),
                (
                    "detail",
                    Value::optional_utf8(detail.map(JsonValue::to_canonical_string)),
                ),
            ],
        )?;
        Ok(marker_id)
    }

    /// Record one event in the optional full event stream.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] on write failure.
    pub fn record_event(&mut self, event: &EventRecord) -> Result<(), RecorderError> {
        self.record_cells(
            Artifact::Events,
            [
                ("event_id", Value::utf8(event.event_id.clone())),
                ("sequence", Value::UInt64(event.sequence)),
                ("timestamp", Value::TimestampNs(event.timestamp_ns)),
                ("event_type", Value::utf8(event.event_type.clone())),
                ("type_name", Value::utf8(event.type_name.clone())),
                ("symbol", Value::optional_utf8(event.symbol.clone())),
                (
                    "payload",
                    Value::optional_utf8(
                        event.payload.as_ref().map(JsonValue::to_canonical_string),
                    ),
                ),
            ],
        )
    }

    /// Finalize every artifact and return the run manifest.
    ///
    /// # Errors
    ///
    /// Returns [`RecorderError`] when an artifact cannot be finalized.
    pub fn close(&mut self) -> Result<RunManifest, RecorderError> {
        if self.closed {
            return self
                .manifest
                .clone()
                .ok_or(RecorderError::ManifestUnavailable);
        }
        let mut row_counts: BTreeMap<String, u64> = BTreeMap::new();
        let mut digests: BTreeMap<String, String> = BTreeMap::new();
        for artifact in STREAM_ARTIFACTS {
            let writer = self
                .writers
                .get_mut(&artifact)
                .expect("writer exists for every stream artifact");
            writer.close()?;
            let name = artifact.value().to_string();
            row_counts.insert(name.clone(), writer.row_count());
            let path = artifact_path(&self.config.directory, artifact);
            let digest = file_digest(&path)
                .map_err(|error| RecorderError::Writer(WriterError::Io(error)))?;
            digests.insert(name, digest);
        }
        let run_digest = run_fingerprint(&row_counts, &digests);
        let rows = manifest_rows(&ManifestRowsInput {
            job_id: &self.config.job_id,
            seed: &self.config.seed.as_text(),
            batch_size: self.config.batch_size,
            compression: &self.config.compression,
            parquet_version: &self.config.parquet_version,
            record_events: self.config.record_events,
            record_payloads: self.config.record_payloads,
            strategy_id: self.config.strategy_id.as_deref().unwrap_or(""),
            book_id: self.config.book_id.as_deref().unwrap_or(""),
            account_id: self.config.account_id.as_deref().unwrap_or(""),
            metadata: &self.config.metadata,
            row_counts: &row_counts,
            digests: &digests,
            run_digest: &run_digest,
        });
        row_counts.insert(Artifact::Manifest.value().to_string(), rows.len() as u64);
        let manifest_path = artifact_path(&self.config.directory, Artifact::Manifest);
        let mut writer =
            ParquetArtifactWriter::new(manifest_path, Artifact::Manifest, rows.len().max(1));
        for (key, value) in &rows {
            let row = build_row(
                Artifact::Manifest,
                [
                    ("key", Value::utf8(key.clone())),
                    ("value", Value::utf8(value.clone())),
                ],
            )?;
            writer.append(&row)?;
        }
        writer.close()?;
        self.closed = true;
        let manifest = RunManifest {
            job_id: self.config.job_id.clone(),
            directory: PathBuf::from(&self.config.directory),
            row_counts: row_counts.clone(),
            digests,
            run_digest,
            metadata: self.config.metadata.clone(),
        };
        self.manifest = Some(manifest.clone());
        Ok(manifest)
    }

    fn next_sequence(&mut self) -> u64 {
        let sequence = self.sequence;
        self.sequence += 1;
        sequence
    }

    fn resolve_timestamp(&self, at_ns: Option<i64>) -> Result<i64, RecorderError> {
        if let Some(value) = at_ns {
            return Ok(value);
        }
        if let Some(clock) = &self.config.clock {
            return Ok(clock.now_ns());
        }
        Err(RecorderError::TimestampRequired)
    }

    fn strategy_id(&self, strategy_id: Option<&str>) -> Result<String, RecorderError> {
        if let Some(value) = strategy_id {
            return Ok(text(value, "strategy_id")?);
        }
        Ok(self.config.strategy_id.clone().unwrap_or_default())
    }

    fn book_id(&self, book_id: Option<&str>) -> Result<Option<String>, RecorderError> {
        if let Some(value) = book_id {
            return Ok(Some(text(value, "book_id")?));
        }
        Ok(self.config.book_id.clone())
    }

    fn account_id(&self, account_id: Option<&str>) -> Result<String, RecorderError> {
        if let Some(value) = account_id {
            return Ok(text(value, "account_id")?);
        }
        self.config
            .account_id
            .clone()
            .ok_or(RecorderError::AccountRequired)
    }
}
