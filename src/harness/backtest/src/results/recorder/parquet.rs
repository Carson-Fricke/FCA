//! Arrow/Parquet artifact sink for the streaming recorder.
//!
//! The Python recorder writes every artifact through `pyarrow`; this module is
//! the staged port's native equivalent. [`ParquetArtifactWriter`] is a
//! [`RowSink`] that encodes schema rows into a real Parquet object with the
//! `arrow-rs`/`parquet` crates, so the `<artifact>.parquet` files are readable
//! by Arrow rather than canonical JSON lines.
//!
//! The Arrow schema mirrors [`Artifact::fields`] exactly: UTF-8, unsigned
//! integers, booleans, and UTC nanosecond timestamps, with the three
//! `harness.schema` metadata keys preserved as Parquet key/value metadata. The
//! serialized Arrow IPC schema is intentionally omitted so the byte layout is a
//! pure function of the rows and the fixed writer properties.

use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::{
    ArrayRef, BooleanArray, RecordBatch, StringArray, TimestampNanosecondArray, UInt32Array,
    UInt64Array,
};
use arrow_schema::{DataType as ArrowDataType, Field, Schema, TimeUnit};
use parquet::arrow::arrow_writer::ArrowWriterOptions;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::metadata::KeyValue;
use parquet::file::properties::{WriterProperties, WriterVersion};

use super::schemas::{Artifact, FieldDef, LogicalType};
use super::writer::{validate, Row, RowSink, Value, WriterError};

/// A [`RowSink`] that writes one artifact as a real Parquet object.
pub struct ParquetArtifactWriter {
    artifact: Artifact,
    path: PathBuf,
    batch_size: usize,
    pending: Vec<Row>,
    row_count: u64,
    schema: Arc<Schema>,
    metadata: Vec<KeyValue>,
    writer: Option<ArrowWriter<BufWriter<File>>>,
}

impl ParquetArtifactWriter {
    /// Create a writer targeting `path` for `artifact`.
    ///
    /// The Parquet file is opened lazily on the first flush.
    #[must_use]
    pub fn new(path: impl Into<PathBuf>, artifact: Artifact, batch_size: usize) -> Self {
        Self {
            artifact,
            path: path.into(),
            batch_size: batch_size.max(1),
            pending: Vec::new(),
            row_count: 0,
            schema: parquet_schema(artifact),
            metadata: parquet_metadata(artifact),
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

    fn require_writer(&mut self) -> Result<&mut ArrowWriter<BufWriter<File>>, WriterError> {
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
            let properties = WriterProperties::builder()
                .set_compression(Compression::ZSTD(Default::default()))
                .set_writer_version(WriterVersion::PARQUET_2_0)
                .set_key_value_metadata(Some(self.metadata.clone()))
                .build();
            let options = ArrowWriterOptions::new()
                .with_properties(properties)
                .with_skip_arrow_metadata(true);
            let writer = ArrowWriter::try_new_with_options(
                BufWriter::new(file),
                Arc::clone(&self.schema),
                options,
            )
            .map_err(io_error)?;
            self.writer = Some(writer);
        }
        Ok(self.writer.as_mut().expect("writer initialized"))
    }
}

impl RowSink for ParquetArtifactWriter {
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
        let columns = build_columns(self.artifact, &self.pending)?;
        let batch = RecordBatch::try_new(Arc::clone(&self.schema), columns).map_err(io_error)?;
        let rows = self.pending.len() as u64;
        let writer = self.require_writer()?;
        writer.write(&batch).map_err(io_error)?;
        self.row_count += rows;
        self.pending.clear();
        Ok(())
    }

    fn close(&mut self) -> Result<(), WriterError> {
        self.flush()?;
        // Materialize the artifact even when it has no rows, matching the
        // Parquet writer the Python recorder opens on close.
        if self.writer.is_none() {
            self.require_writer()?;
        }
        if let Some(writer) = self.writer.take() {
            writer.close().map_err(io_error)?;
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

fn io_error(error: impl std::error::Error + Send + Sync + 'static) -> WriterError {
    WriterError::Io(io::Error::other(error))
}

fn arrow_type(logical_type: LogicalType) -> ArrowDataType {
    match logical_type {
        LogicalType::Utf8 => ArrowDataType::Utf8,
        LogicalType::UInt32 => ArrowDataType::UInt32,
        LogicalType::UInt64 => ArrowDataType::UInt64,
        LogicalType::Bool => ArrowDataType::Boolean,
        LogicalType::TimestampNsUtc => {
            ArrowDataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()))
        }
    }
}

fn parquet_schema(artifact: Artifact) -> Arc<Schema> {
    let fields: Vec<Field> = artifact
        .fields()
        .iter()
        .map(|field| Field::new(field.name, arrow_type(field.logical_type), field.nullable))
        .collect();
    Arc::new(Schema::new(fields))
}

fn parquet_metadata(artifact: Artifact) -> Vec<KeyValue> {
    artifact
        .metadata()
        .into_iter()
        .map(|(key, value)| KeyValue {
            key: key.to_string(),
            value: Some(value),
        })
        .collect()
}

fn build_columns(artifact: Artifact, rows: &[Row]) -> Result<Vec<ArrayRef>, WriterError> {
    artifact
        .fields()
        .iter()
        .enumerate()
        .map(|(index, field)| build_column(field, rows, index))
        .collect()
}

fn build_column(field: &FieldDef, rows: &[Row], index: usize) -> Result<ArrayRef, WriterError> {
    match field.logical_type {
        LogicalType::Utf8 => {
            let mut values: Vec<Option<String>> = Vec::with_capacity(rows.len());
            for row in rows {
                match &row[index] {
                    Value::Utf8(text) => values.push(Some(text.clone())),
                    Value::Null => values.push(None),
                    Value::UInt32(_) | Value::UInt64(_) | Value::Bool(_) | Value::TimestampNs(_) => {
                        return Err(mismatch(field));
                    }
                }
            }
            Ok(Arc::new(StringArray::from(values)))
        }
        LogicalType::UInt32 => {
            let mut values: Vec<Option<u32>> = Vec::with_capacity(rows.len());
            for row in rows {
                match &row[index] {
                    Value::UInt32(value) => values.push(Some(*value)),
                    Value::Null => values.push(None),
                    _ => return Err(mismatch(field)),
                }
            }
            Ok(Arc::new(UInt32Array::from(values)))
        }
        LogicalType::UInt64 => {
            let mut values: Vec<Option<u64>> = Vec::with_capacity(rows.len());
            for row in rows {
                match &row[index] {
                    Value::UInt64(value) => values.push(Some(*value)),
                    Value::Null => values.push(None),
                    _ => return Err(mismatch(field)),
                }
            }
            Ok(Arc::new(UInt64Array::from(values)))
        }
        LogicalType::Bool => {
            let mut values: Vec<Option<bool>> = Vec::with_capacity(rows.len());
            for row in rows {
                match &row[index] {
                    Value::Bool(value) => values.push(Some(*value)),
                    Value::Null => values.push(None),
                    _ => return Err(mismatch(field)),
                }
            }
            Ok(Arc::new(BooleanArray::from(values)))
        }
        LogicalType::TimestampNsUtc => {
            let mut values: Vec<Option<i64>> = Vec::with_capacity(rows.len());
            for row in rows {
                match &row[index] {
                    Value::TimestampNs(value) => values.push(Some(*value)),
                    Value::Null => values.push(None),
                    _ => return Err(mismatch(field)),
                }
            }
            let array = TimestampNanosecondArray::from(values).with_timezone("UTC".to_string());
            Ok(Arc::new(array))
        }
    }
}

fn mismatch(field: &FieldDef) -> WriterError {
    WriterError::TypeMismatch {
        field: field.name,
        expected: field.logical_type.name(),
    }
}

#[cfg(test)]
mod tests {
    use std::io::Read as _;

    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

    use super::*;
    use crate::results::recorder::writer::build_row;

    fn temp_dir(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "harness-backtest-parquet-{}-{name}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|value| value.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&path).expect("temp dir");
        path
    }

    #[test]
    fn writes_arrow_readable_parquet_with_schema_metadata() {
        let directory = temp_dir("roundtrip");
        let path = directory.join("manifest.parquet");
        let mut writer = ParquetArtifactWriter::new(&path, Artifact::Manifest, 2);
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

        let mut bytes = Vec::new();
        std::fs::File::open(&path)
            .expect("open")
            .read_to_end(&mut bytes)
            .expect("read");
        assert_eq!(&bytes[..4], b"PAR1");

        let builder =
            ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&path).expect("open"))
                .expect("arrow reader");
        assert_eq!(builder.metadata().file_metadata().num_rows(), 5);
        let names: Vec<&str> = builder
            .schema()
            .fields()
            .iter()
            .map(|field| field.name().as_str())
            .collect();
        assert_eq!(names, vec!["key", "value"]);
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn empty_artifact_still_materializes() {
        let directory = temp_dir("empty");
        let path = directory.join("events.parquet");
        let mut writer = ParquetArtifactWriter::new(&path, Artifact::Events, 8);
        writer.close().expect("close");
        assert!(path.exists());
        let builder =
            ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&path).expect("open"))
                .expect("arrow reader");
        assert_eq!(builder.metadata().file_metadata().num_rows(), 0);
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn schema_carries_the_harness_metadata() {
        let directory = temp_dir("metadata");
        let path = directory.join("equity.parquet");
        let mut writer = ParquetArtifactWriter::new(&path, Artifact::Equity, 8);
        let row = build_row(
            Artifact::Equity,
            [
                ("snapshot_id", Value::utf8("s")),
                ("sequence", Value::UInt64(0)),
                ("timestamp", Value::TimestampNs(0)),
                ("account_id", Value::utf8("a")),
                ("source", Value::utf8("portfolio")),
                ("cash", Value::utf8("0")),
                ("realized_pnl", Value::utf8("0")),
                ("position_count", Value::UInt32(0)),
                ("fill_count", Value::UInt64(0)),
            ],
        )
        .expect("row");
        writer.append(&row).expect("append");
        writer.close().expect("close");
        let builder =
            ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&path).expect("open"))
                .expect("arrow reader");
        let metadata = builder.metadata().file_metadata().key_value_metadata();
        let keys: Vec<&str> = metadata
            .map(|entries| entries.iter().map(|entry| entry.key.as_str()).collect())
            .unwrap_or_default();
        assert!(keys.contains(&"harness.schema"), "{keys:?}");
        assert!(keys.contains(&"harness.decimal.encoding"), "{keys:?}");
        let _ = std::fs::remove_dir_all(&directory);
    }
}
