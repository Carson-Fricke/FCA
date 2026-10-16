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
use sha2::{Digest, Sha256};

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

/// Serialize a canonical Arrow batch and replace its deterministic object.
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
        validate_batch(batch, asset_class, data_type, symbol)?;
        let schema = batch.schema();
        let metadata = schema.metadata();
        let name = metadata
            .get(SCHEMA_METADATA_KEY)
            .cloned()
            .unwrap_or_default();
        let version = metadata
            .get(SCHEMA_VERSION_METADATA_KEY)
            .cloned()
            .unwrap_or_default();
        let properties = WriterProperties::builder()
            .set_compression(Compression::ZSTD(Default::default()))
            .set_writer_version(WriterVersion::PARQUET_2_0)
            .set_key_value_metadata(Some(vec![
                KeyValue::new(SCHEMA_METADATA_KEY.to_string(), name),
                KeyValue::new(SCHEMA_VERSION_METADATA_KEY.to_string(), version),
            ]))
            .build();
        let mut payload = Vec::new();
        let encode_started = std::time::Instant::now();
        {
            let mut writer = ArrowWriter::try_new(&mut payload, schema, Some(properties))
                .map_err(DataError::io)?;
            writer.write(batch).map_err(DataError::io)?;
            writer.close().map_err(DataError::io)?;
        }
        let encode_seconds = encode_started.elapsed().as_secs_f64();
        let path = partition_path(session_date, asset_class, data_type, interval, symbol)?;
        let checksum_started = std::time::Instant::now();
        let checksum = hex::encode(Sha256::digest(&payload));
        self.store.replace(&path, &payload)?;
        eprintln!(
            "PROFILE parquet rows={} encode_seconds={encode_seconds:.3} checksum_store_seconds={:.3} bytes={}",
            batch.num_rows(),
            checksum_started.elapsed().as_secs_f64(),
            payload.len()
        );
        Ok(ParquetMetadata {
            path,
            rows: batch.num_rows() as u64,
            bytes: payload.len() as u64,
            checksum,
        })
    }
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
