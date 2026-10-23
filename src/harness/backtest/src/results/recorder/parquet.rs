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

use std::fs::File;
use std::fs::OpenOptions;
use std::io;
use std::io::BufWriter;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;

use arrow_array::RecordBatch;
use arrow_schema::DataType as ArrowDataType;
use arrow_schema::Field;
use arrow_schema::Schema;
use arrow_schema::TimeUnit;
use parquet::arrow::arrow_writer::ArrowWriterOptions;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;
use parquet::file::properties::WriterVersion;

use super::columns::Cell;
use super::columns::Columns;
use super::schemas::Artifact;
use super::schemas::LogicalType;
use super::writer::validate;
use super::writer::Row;
use super::writer::RowSink;
use super::writer::WriterError;

/// A [`RowSink`] that writes one artifact as a real Parquet object.
pub struct ParquetArtifactWriter {
    artifact: Artifact,
    path: PathBuf,
    batch_size: usize,
    pending: Columns,
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
            pending: Columns::new(artifact),
            row_count: 0,
            schema: parquet_schema(artifact),
            metadata: parquet_metadata(artifact),
            writer: None,
        }
    }

    pub(super) fn append_cells<'a>(
        &mut self,
        cells: impl IntoIterator<Item = Cell<'a>>,
    ) -> Result<(), WriterError> {
        self.pending.append(cells)?;
        if self.pending.rows >= self.batch_size {
            self.flush()?;
        }
        Ok(())
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
        self.pending.append(row.iter().map(Cell::from))?;
        if self.pending.rows >= self.batch_size {
            self.flush()?;
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<(), WriterError> {
        if self.pending.rows == 0 {
            return Ok(());
        }
        let rows = self.pending.rows as u64;
        let columns = self.pending.finish();
        let batch = RecordBatch::try_new(Arc::clone(&self.schema), columns).map_err(io_error)?;
        let writer = self.require_writer()?;
        writer.write(&batch).map_err(io_error)?;
        self.row_count += rows;
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
        self.pending.rows
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
