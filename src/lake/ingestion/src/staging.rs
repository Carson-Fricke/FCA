//! File-backed unpublished Parquet staging; reset releases owned scratch artifacts.
use crate::common::{PartitionWriter, StagedParquetObject};
use arrow_array::RecordBatch;
use lake_contracts::{AssetClass, DataError, DataType, Date};
use lake_storage::ParquetMetadata;
use std::sync::{Mutex, MutexGuard};
#[derive(Default)]
pub struct StagingPartitionWriter {
    staged: Mutex<Vec<StagedParquetObject>>,
}
impl StagingPartitionWriter {
    pub fn new() -> Self {
        Self::default()
    }
}
impl PartitionWriter for StagingPartitionWriter {
    fn write(
        &self,
        records: &RecordBatch,
        day: Date,
        asset: AssetClass,
        data: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        self.write_batches(
            Box::new(std::iter::once(Ok(records.clone()))),
            day,
            asset,
            data,
            interval,
            symbol,
        )
    }
    fn write_batches(
        &self,
        records: Box<dyn Iterator<Item = Result<RecordBatch, DataError>> + Send>,
        day: Date,
        asset: AssetClass,
        data: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let (metadata, payload) =
            lake_storage::parquet::stage_batches(records, day, asset, data, interval, symbol)?;
        lock(&self.staged).push(StagedParquetObject {
            metadata: metadata.clone(),
            payload,
        });
        Ok(metadata)
    }
    fn drain_staged(&self) -> Vec<StagedParquetObject> {
        std::mem::take(&mut *lock(&self.staged))
    }
    fn reset_staged(&self) {
        lock(&self.staged).clear();
    }
}
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}
