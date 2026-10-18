//! Idempotent, schema-preserving Parquet partition writes.
//!
//! Validated Arrow batches in the pinned schema from
//! `lake_contracts::schema_for` are serialized as one zstd-compressed
//! Parquet object with `PARQUET_2_0` metadata, and committed through the
//! whole-object [`BlobStore`] boundary. Row order is preserved.

use std::sync::Arc;

use arrow_array::{Array, Decimal128Array, ListArray, RecordBatch, StringArray};
use lake_contracts::schemas::{SCHEMA_METADATA_KEY, SCHEMA_VERSION_METADATA_KEY};
use lake_contracts::{
    partition_path, schema_for, symbol_filename, AssetClass, DataError, DataType, Date,
};
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::metadata::KeyValue;
use parquet::file::properties::{WriterProperties, WriterVersion};

use crate::ports::BlobStore;

/// Metadata returned for the exact bytes committed to an object store.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParquetMetadata {
    /// The repository-relative object path.
    pub path: String,
    /// The number of rows in the object.
    pub rows: u64,
    /// The payload size in bytes.
    pub bytes: u64,
    /// The lowercase sha256 hex digest of the payload.
    pub checksum: String,
}

impl ParquetMetadata {
    /// The repository-relative object path.
    pub fn object_path(&self) -> &str {
        &self.path
    }
}

/// Serialize a canonical Arrow batch under its immutable content-addressed path.
pub struct ParquetWriter<S: BlobStore> {
    store: Arc<S>,
}

impl<S: BlobStore + 'static> ParquetWriter<S> {
    /// Build a writer over a shared object store.
    pub fn new(store: Arc<S>) -> Self {
        Self { store }
    }

    /// Write one zstd-compressed Parquet object and return its metadata.
    ///
    /// `interval` carries the bars interval partition dimension; every other
    /// data type must pass `None`, and bars must pass a canonical token so the
    /// frozen `.../data_type=bars/interval=<token>/SYMBOL.parquet` path is
    /// produced.
    pub fn write(
        &self,
        batch: &RecordBatch,
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let (metadata, file) = stage_batches(
            std::iter::once(Ok(batch.clone())),
            session_date,
            asset_class,
            data_type,
            interval,
            symbol,
        )?;
        self.store.replace_file(&metadata.path, file.path())?;
        Ok(metadata)
    }
}

/// Encode a bounded batch iterator to an owned scratch file. Publication belongs to the caller.
pub fn stage_batches(
    batches: impl Iterator<Item = Result<RecordBatch, DataError>>,
    session_date: Date,
    asset_class: AssetClass,
    data_type: DataType,
    interval: Option<&str>,
    symbol: &str,
) -> Result<(ParquetMetadata, Arc<crate::StagedFile>), DataError> {
    let schema = Arc::new(schema_for(asset_class, data_type)?);
    let properties = WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .set_writer_version(WriterVersion::PARQUET_2_0)
        .set_max_row_group_row_count(Some(131072))
        .set_key_value_metadata(Some(vec![
            KeyValue::new(
                SCHEMA_METADATA_KEY.to_string(),
                schema.metadata().get(SCHEMA_METADATA_KEY).cloned(),
            ),
            KeyValue::new(
                SCHEMA_VERSION_METADATA_KEY.to_string(),
                schema.metadata().get(SCHEMA_VERSION_METADATA_KEY).cloned(),
            ),
        ]))
        .build();
    let pipeline = lake_pipeline::Pipeline::global();
    let mut file = crate::StagedFile::new()?;
    let mut rows = 0;
    {
        let mut writer =
            ArrowWriter::try_new(&mut file, schema, Some(properties)).map_err(DataError::io)?;
        for batch in batches {
            pipeline.check()?;
            let batch = batch?;
            validate_batch(&batch, asset_class, data_type, symbol)?;
            rows += batch.num_rows();
            let mut metric = pipeline.timer(lake_pipeline::Stage::Parquet);
            metric.rows(batch.num_rows());
            writer.write(&batch).map_err(DataError::io)?;
        }
        let _close = pipeline.timer(lake_pipeline::Stage::Parquet);
        writer.close().map_err(DataError::io)?;
    }

    let mut hash = pipeline.timer(lake_pipeline::Stage::Hash);
    hash.bytes(file.len());
    let checksum = file.checksum()?;
    let logical = partition_path(session_date, asset_class, data_type, interval, symbol)?;
    let path = lake_contracts::immutable_path(&logical, &checksum)?;
    let metadata = ParquetMetadata {
        path,
        rows: rows as u64,
        bytes: file.len() as u64,
        checksum,
    };
    Ok((metadata, Arc::new(file.seal())))
}

/// Reject a schema or partition mismatch before serialization or mutation.
fn validate_batch(
    batch: &RecordBatch,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> Result<(), DataError> {
    let expected = schema_for(asset_class, data_type)?;
    if batch.schema().as_ref() != &expected {
        return Err(DataError::value(
            "batch does not match the current lake schema",
        ));
    }
    for (field, column) in expected.fields().iter().zip(batch.columns()) {
        if !field.is_nullable() && column.null_count() != 0 {
            return Err(DataError::value(format!(
                "required field '{}' contains nulls",
                field.name()
            )));
        }
        if let Some(values) = column.as_any().downcast_ref::<Decimal128Array>() {
            values
                .validate_decimal_precision(38)
                .map_err(DataError::io)?;
        }
        if let Some(values) = column.as_any().downcast_ref::<ListArray>() {
            if values.values().null_count() != 0 {
                return Err(DataError::value("condition lists contain null values"));
            }
        }
    }
    let canonical = symbol.trim().to_uppercase();
    symbol_filename(&canonical)?;
    let symbols = batch
        .column_by_name("symbol")
        .and_then(|column| column.as_any().downcast_ref::<StringArray>())
        .ok_or_else(|| DataError::value("batch has no canonical symbol column"))?;
    if symbols
        .iter()
        .any(|value| value != Some(canonical.as_str()))
    {
        return Err(DataError::value(
            "batch symbol does not match partition symbol",
        ));
    }
    Ok(())
}
