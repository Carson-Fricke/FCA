//! Streaming run recorder and bounded-memory artifact trail.
//!
//! A [`RunRecorder`] turns one deterministic run into referentially linked
//! artifacts. The staged Rust port preserves the Python field order, schema
//! metadata, identifier derivation, and manifest digest framing, and writes each
//! artifact through the [`RowSink`] seam. The production sink is
//! [`ParquetArtifactWriter`], which encodes real Arrow/Parquet objects; the
//! canonical JSON [`CanonicalArtifactWriter`] remains for schema-faithful
//! reference output and tests.

pub mod artifacts;
mod columns;
pub mod config;
pub mod ids;
pub mod manifest;
pub mod parquet;
pub mod run;
pub mod schemas;
pub mod writer;

pub use artifacts::artifact_path;
pub use config::{
    text, ActionKind, Clock, ConfigError, FixedClock, MarkerScope, RecorderConfig, Seed,
    TransitionReason,
};
pub use ids::{EngineIdSource, IdError};
pub use manifest::{
    digest_bytes, file_digest, manifest_rows, run_fingerprint, truth_text, ManifestRowsInput,
    RunManifest,
};
pub use parquet::ParquetArtifactWriter;
pub use run::{
    ActionRecord, EquityRecord, EventRecord, ExecutionReportRecord, FillRecord,
    PortfolioEquityRecord, PositionTransitionRecord, RecorderError, RunRecorder,
};
pub use schemas::{
    Artifact, FieldDef, LogicalType, DECIMAL_ENCODING, SCHEMA_VERSION, STREAM_ARTIFACTS,
};
pub use writer::{
    build_row, format_timestamp_ns, row_to_canonical_json, CanonicalArtifactWriter, Row, RowSink,
    Value, WriterError,
};

/// Recorded artifact file names, keyed by artifact.
pub static ARTIFACT_FILES: [(&str, &str); 12] = [
    ("actions", "actions.parquet"),
    ("orders", "orders.parquet"),
    ("risk_decisions", "risk_decisions.parquet"),
    ("risk_audits", "risk_audits.parquet"),
    ("fills", "fills.parquet"),
    ("execution_reports", "execution_reports.parquet"),
    ("position_transitions", "position_transitions.parquet"),
    ("equity", "equity.parquet"),
    ("emissions", "emissions.parquet"),
    ("markers", "markers.parquet"),
    ("events", "events.parquet"),
    ("manifest", "manifest.parquet"),
];
