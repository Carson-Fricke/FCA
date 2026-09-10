//! Deterministic artifact writing and row serialization.
//!
//! The Python recorder streams rows into Parquet through `pyarrow`. The staged
//! Rust port has no Arrow/native dependency yet, so this module defines the
//! [`RowSink`] seam plus a canonical, byte-stable writer: every artifact file
//! is the concatenation of one canonical JSON object per row (sorted keys, no
//! insignificant whitespace, ASCII-only escapes, string-exact decimals, and
//! RFC3339 microsecond timestamps). The logical field order and values match
//! the Parquet writer exactly, so an Arrow-backed [`RowSink`] can replace
//! [`CanonicalArtifactWriter`] without touching the recorder.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};

use crate::canonical_json::JsonValue;

use super::schemas::{Artifact, FieldDef, LogicalType};

/// One recorded cell value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    /// A null value.
    Null,
    /// A UTF-8 string (decimals are string-exact).
    Utf8(String),
    /// An unsigned 32-bit integer.
    UInt32(u32),
    /// An unsigned 64-bit integer.
    UInt64(u64),
    /// A boolean.
    Bool(bool),
    /// A nanosecond timestamp since the Unix epoch, UTC.
    TimestampNs(i64),
}

impl Value {
    /// Build a string cell.
    #[must_use]
    pub fn utf8(value: impl Into<String>) -> Self {
        Self::Utf8(value.into())
    }

    /// Build an optional string cell from an `Option`.
    #[must_use]
    pub fn optional_utf8(value: Option<impl Into<String>>) -> Self {
        match value {
            Some(value) => Self::Utf8(value.into()),
            None => Self::Null,
        }
    }

    /// Whether this value is null.
    #[must_use]
    pub const fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    fn to_json(&self) -> JsonValue {
        match self {
            Self::Null => JsonValue::Null,
            Self::Utf8(value) => JsonValue::String(value.clone()),
            Self::UInt32(value) => JsonValue::UInt(u64::from(*value)),
            Self::UInt64(value) => JsonValue::UInt(*value),
            Self::Bool(value) => JsonValue::Bool(*value),
            Self::TimestampNs(value) => JsonValue::String(format_timestamp_ns(*value)),
        }
    }
}

/// One artifact row, aligned to the artifact's schema field order.
pub type Row = Vec<Value>;

/// Build a schema-ordered row from named cells.
///
/// # Errors
///
/// Returns [`WriterError::UnknownField`] when a supplied field is not part of
/// the artifact schema, or [`WriterError::MissingField`] when a schema field is
/// absent from `cells`.
pub fn build_row(
    artifact: Artifact,
    cells: impl IntoIterator<Item = (&'static str, Value)>,
) -> Result<Row, WriterError> {
    let supplied: BTreeMap<&str, Value> = cells.into_iter().collect();
    let fields = artifact.fields();
    for name in supplied.keys() {
        if !fields.iter().any(|field| field.name == *name) {
            return Err(WriterError::UnknownField {
                artifact,
                field: name,
            });
        }
    }
    let mut row = Vec::with_capacity(fields.len());
    for field in fields {
        match supplied.get(field.name) {
            Some(value) => {
                validate(field, value)?;
                row.push(value.clone());
            }
            None => {
                if !field.nullable {
                    return Err(WriterError::MissingField {
                        artifact,
                        field: field.name,
                    });
                }
                row.push(Value::Null);
            }
        }
    }
    Ok(row)
}

fn validate(field: &FieldDef, value: &Value) -> Result<(), WriterError> {
    if value.is_null() {
        if field.nullable {
            return Ok(());
        }
        return Err(WriterError::NullNotAllowed { field: field.name });
    }
    let matches = matches!(
        (field.logical_type, value),
        (LogicalType::Utf8, Value::Utf8(_))
            | (LogicalType::UInt32, Value::UInt32(_))
            | (LogicalType::UInt64, Value::UInt64(_))
            | (LogicalType::Bool, Value::Bool(_))
            | (LogicalType::TimestampNsUtc, Value::TimestampNs(_))
    );
    if matches {
        Ok(())
    } else {
        Err(WriterError::TypeMismatch {
            field: field.name,
            expected: field.logical_type.name(),
        })
    }
}

/// Serialize one row as a canonical JSON object using schema field names.
#[must_use]
pub fn row_to_canonical_json(artifact: Artifact, row: &Row) -> String {
    debug_assert_eq!(artifact.fields().len(), row.len());
    let mut map = BTreeMap::new();
    for (field, value) in artifact.fields().iter().zip(row.iter()) {
        map.insert(field.name.to_string(), value.to_json());
    }
    JsonValue::Object(map).to_canonical_string()
}

/// Format nanosecond epoch time as RFC3339 with microsecond precision and `Z`.
///
/// Mirrors `datetime.isoformat(timespec="microseconds")` for a UTC instant:
/// nanoseconds are truncated to microseconds, not rounded.
#[must_use]
pub fn format_timestamp_ns(timestamp_ns: i64) -> String {
    let micros_total = timestamp_ns.div_euclid(1_000);
    let seconds = micros_total.div_euclid(1_000_000);
    let reminder_micros = micros_total.rem_euclid(1_000_000);
    let days = seconds.div_euclid(86_400);
    let seconds_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = seconds_of_day / 3_600;
    let minute = (seconds_of_day % 3_600) / 60;
    let second = seconds_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{reminder_micros:06}Z")
}

/// Convert a day count since 1970-01-01 to a proleptic Gregorian date.
///
/// Howard Hinnant's `civil_from_days` algorithm, valid for the full `i64`
/// range used by the harness epoch timestamps.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year + 1 } else { year };
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    (year, month as u32, day as u32)
}

/// A sink that accepts schema rows for one artifact.
///
/// The seam lets the Parquet/Arrow writer land later without changing the
/// recorder. Implementations must be deterministic and must not read the wall
/// clock, the network, or an unseeded random source.
pub trait RowSink {
    /// Buffer or write one row.
    ///
    /// # Errors
    ///
    /// Returns [`WriterError`] when the row is invalid or the sink fails.
    fn append(&mut self, row: &Row) -> Result<(), WriterError>;

    /// Flush any buffered rows.
    ///
    /// # Errors
    ///
    /// Returns [`WriterError`] when the sink fails.
    fn flush(&mut self) -> Result<(), WriterError>;

    /// Flush buffered rows and finalize the artifact.
    ///
    /// # Errors
    ///
    /// Returns [`WriterError`] when the sink fails.
    fn close(&mut self) -> Result<(), WriterError>;

    /// Return rows already flushed to the artifact.
    fn row_count(&self) -> u64;

    /// Return rows buffered in memory and not yet flushed.
    fn pending_rows(&self) -> usize;
}

/// Canonical, byte-stable artifact writer.
pub struct CanonicalArtifactWriter {
    artifact: Artifact,
    path: PathBuf,
    batch_size: usize,
    pending: Vec<Row>,
    row_count: u64,
    writer: Option<BufWriter<File>>,
}

impl CanonicalArtifactWriter {
    /// Create a writer targeting `path` for `artifact`.
    ///
    /// The file is opened lazily on the first flush.
    #[must_use]
    pub fn new(path: impl Into<PathBuf>, artifact: Artifact, batch_size: usize) -> Self {
        Self {
            artifact,
            path: path.into(),
            batch_size: batch_size.max(1),
            pending: Vec::new(),
            row_count: 0,
            writer: None,
        }
    }

    /// The artifact written by this sink.
    #[must_use]
    pub const fn artifact(&self) -> Artifact {
        self.artifact
    }

    /// The target path.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    fn require_writer(&mut self) -> io::Result<&mut BufWriter<File>> {
        if self.writer.is_none() {
            if let Some(parent) = self.path.parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent)?;
                }
            }
            let file = OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(true)
                .open(&self.path)?;
            self.writer = Some(BufWriter::new(file));
        }
        Ok(self.writer.as_mut().expect("writer initialized"))
    }
}

impl RowSink for CanonicalArtifactWriter {
    fn append(&mut self, row: &Row) -> Result<(), WriterError> {
        let fields = self.artifact.fields();
        if row.len() != fields.len() {
            return Err(WriterError::Schema {
                artifact: self.artifact,
                expected: fields.len(),
                got: row.len(),
            });
        }
        for (field, value) in fields.iter().zip(row.iter()) {
            validate(field, value)?;
        }
        self.pending.push(row.clone());
        if self.pending.len() >= self.batch_size {
            self.flush()?;
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<(), WriterError> {
        if self.pending.is_empty() {
            return Ok(());
        }
        let mut payload = String::new();
        for row in &self.pending {
            payload.push_str(&row_to_canonical_json(self.artifact, row));
            payload.push('\n');
        }
        let writer = self.require_writer()?;
        writer.write_all(payload.as_bytes())?;
        self.row_count += payload.matches('\n').count() as u64;
        self.pending.clear();
        Ok(())
    }

    fn close(&mut self) -> Result<(), WriterError> {
        self.flush()?;
        // Materialize the artifact even when it has no rows, matching the
        // Parquet writer the Python recorder opens on close.
        self.require_writer()?;
        if let Some(mut writer) = self.writer.take() {
            writer.flush()?;
        }
        Ok(())
    }

    fn row_count(&self) -> u64 {
        self.row_count
    }

    fn pending_rows(&self) -> usize {
        self.pending.len()
    }
}

/// Row or artifact write failure.
#[derive(Debug)]
pub enum WriterError {
    /// An I/O failure.
    Io(io::Error),
    /// The row width did not match the artifact schema.
    Schema {
        /// Artifact being written.
        artifact: Artifact,
        /// Expected field count.
        expected: usize,
        /// Supplied field count.
        got: usize,
    },
    /// A non-nullable field was omitted.
    MissingField {
        /// Artifact being written.
        artifact: Artifact,
        /// Missing field.
        field: &'static str,
    },
    /// A supplied field is not part of the artifact schema.
    UnknownField {
        /// Artifact being written.
        artifact: Artifact,
        /// Unknown field.
        field: &'static str,
    },
    /// A null value was supplied for a non-nullable field.
    NullNotAllowed {
        /// Field that rejected null.
        field: &'static str,
    },
    /// A value did not match the field's logical type.
    TypeMismatch {
        /// Field name.
        field: &'static str,
        /// Expected logical type.
        expected: &'static str,
    },
}

impl std::fmt::Display for WriterError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "{error}"),
            Self::Schema {
                artifact,
                expected,
                got,
            } => write!(
                formatter,
                "{} row has {got} fields, expected {expected}",
                artifact.value()
            ),
            Self::MissingField { artifact, field } => {
                write!(
                    formatter,
                    "{} row is missing field {field}",
                    artifact.value()
                )
            }
            Self::UnknownField { artifact, field } => {
                write!(
                    formatter,
                    "{} row has unknown field {field}",
                    artifact.value()
                )
            }
            Self::NullNotAllowed { field } => write!(formatter, "field {field} must not be null"),
            Self::TypeMismatch { field, expected } => {
                write!(formatter, "field {field} must be a {expected} value")
            }
        }
    }
}

impl std::error::Error for WriterError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for WriterError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_epoch_and_pre_epoch_timestamps() {
        assert_eq!(format_timestamp_ns(0), "1970-01-01T00:00:00.000000Z");
        assert_eq!(
            format_timestamp_ns(1_704_153_600_000_000_000),
            "2024-01-02T00:00:00.000000Z"
        );
        assert_eq!(
            format_timestamp_ns(1_704_153_600_123_456_000),
            "2024-01-02T00:00:00.123456Z"
        );
        assert_eq!(format_timestamp_ns(-1), "1969-12-31T23:59:59.999999Z");
    }

    #[test]
    fn row_json_is_schema_ordered_and_sorted() {
        let row = build_row(
            Artifact::Manifest,
            [("value", Value::utf8("x")), ("key", Value::utf8("a"))],
        )
        .expect("row");
        assert_eq!(
            row_to_canonical_json(Artifact::Manifest, &row),
            r#"{"key":"a","value":"x"}"#
        );
    }

    #[test]
    fn missing_non_nullable_field_is_rejected() {
        // `sequence` is required and not supplied.
        let result = build_row(Artifact::Actions, [("action_id", Value::utf8("a"))]);
        assert!(matches!(result, Err(WriterError::MissingField { .. })));
    }

    #[test]
    fn null_not_allowed_is_rejected() {
        let result = build_row(
            Artifact::Actions,
            [
                ("action_id", Value::utf8("a")),
                ("sequence", Value::UInt64(1)),
                ("timestamp", Value::TimestampNs(0)),
                ("strategy_id", Value::utf8("s")),
                ("kind", Value::Null),
            ],
        );
        assert!(matches!(result, Err(WriterError::NullNotAllowed { .. })));
    }

    #[test]
    fn append_and_close_batches_rows() {
        let directory =
            std::env::temp_dir().join(format!("harness-backtest-writer-{}", std::process::id()));
        let path = directory.join("manifest.parquet");
        let mut writer = CanonicalArtifactWriter::new(&path, Artifact::Manifest, 2);
        for index in 0..5u64 {
            let row = build_row(
                Artifact::Manifest,
                [
                    ("key", Value::utf8(format!("k{index}"))),
                    ("value", Value::utf8(index.to_string())),
                ],
            )
            .expect("row");
            writer.append(&row).expect("append");
        }
        assert_eq!(writer.row_count(), 4);
        assert_eq!(writer.pending_rows(), 1);
        writer.close().expect("close");
        assert_eq!(writer.row_count(), 5);
        let text = std::fs::read_to_string(&path).expect("read");
        assert_eq!(text.lines().count(), 5);
        let _ = std::fs::remove_dir_all(&directory);
    }
}
