//! Verified Parquet decoding; sources supply bytes, never provider execution.

use crate::cache::{CachedPartition, PartitionCache};
use crate::{CacheConfig, MarketDataSnapshot, Partition, ReaderError};
use arrow_array::RecordBatch;
use bytes::Bytes;
use futures_util::future::BoxFuture;
use lake_contracts::schema_for;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use sha2::{Digest, Sha256};
use std::sync::{Arc, OnceLock};

pub trait PartitionSource: Send + Sync {
    fn read<'a>(&'a self, partition: &'a Partition) -> BoxFuture<'a, Result<Bytes, ReaderError>>;
}

#[derive(Clone)]
pub struct MarketDataReader {
    cache: Arc<PartitionCache>,
    slots: Arc<tokio::sync::Semaphore>,
}

impl std::fmt::Debug for MarketDataReader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MarketDataReader")
            .field("config", &self.cache.config)
            .finish()
    }
}

impl MarketDataReader {
    pub fn new(config: CacheConfig) -> Self {
        Self {
            cache: Arc::new(PartitionCache::new(config)),
            slots: Arc::new(tokio::sync::Semaphore::new(4)),
        }
    }

    pub fn configured() -> Result<Self, ReaderError> {
        static READER: OnceLock<MarketDataReader> = OnceLock::new();
        let config = CacheConfig::from_env()?;
        let reader = READER.get_or_init(|| Self::new(config.clone()));
        if reader.cache.config != config {
            return Err(ReaderError::Cache(
                "process cache configuration changed".into(),
            ));
        }
        Ok(reader.clone())
    }

    /// Visit bounded batches in snapshot order; oversized partitions are never retained decoded.
    pub async fn visit<F>(
        &self,
        snapshot: &MarketDataSnapshot,
        source: &dyn PartitionSource,
        mut visitor: F,
    ) -> Result<(), ReaderError>
    where
        F: FnMut(RecordBatch) -> Result<(), ReaderError> + Send,
    {
        snapshot.validate()?;
        let (asset, data) = snapshot.request.validate()?;
        let schema = schema_for(asset, data).map_err(|e| ReaderError::Decode(e.to_string()))?;
        for partition in &snapshot.files {
            let key = format!(
                "{}:{}:{}:{}:{}",
                snapshot.request.asset_class,
                snapshot.request.data_type,
                partition.checksum,
                partition.row_count,
                partition.byte_size
            );
            let flight = self.cache.flight(&key);
            let _guard = flight.lock().await;
            if let Some(cached) = self.cache.get(&key) {
                for batch in cached.batches.iter() {
                    visitor(batch.clone())?;
                }
                continue;
            }
            let _slot = self
                .slots
                .acquire()
                .await
                .map_err(|e| ReaderError::Source(e.to_string()))?;
            let cache = self.cache.clone();
            let checksum = partition.checksum.clone();
            let disk = tokio::task::spawn_blocking(move || cache.read_disk(&checksum))
                .await
                .map_err(|e| ReaderError::Cache(e.to_string()))??;
            let payload = match disk {
                Some(bytes) => bytes,
                None => source.read(partition).await?,
            };
            let cache = self.cache.clone();
            let partition = partition.clone();
            let schema = schema.clone();
            let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
            let decode = tokio::task::spawn_blocking(move || {
                if payload.len() as u64 != partition.byte_size
                    || hex::encode(Sha256::digest(&payload)) != partition.checksum
                {
                    return Err(ReaderError::Integrity(partition.object_path));
                }
                let builder = ParquetRecordBatchReaderBuilder::try_new(payload.clone())
                    .map_err(|e| ReaderError::Decode(e.to_string()))?;
                if builder.schema().as_ref() != &schema
                    || builder.metadata().file_metadata().num_rows() as u64 != partition.row_count
                {
                    return Err(ReaderError::Integrity(partition.object_path));
                }
                cache.write_disk(&partition.checksum, &payload)?;
                let mut retained = Some(Vec::new());
                let mut bytes = 0usize;
                for batch in builder
                    .with_batch_size(8192)
                    .build()
                    .map_err(|e| ReaderError::Decode(e.to_string()))?
                {
                    let batch = batch.map_err(|e| ReaderError::Decode(e.to_string()))?;
                    bytes = bytes.saturating_add(batch.get_array_memory_size());
                    // Bound aggregate candidates as well as retained LRU entries.
                    if bytes > cache.config.memory_bytes / 4 {
                        retained = None;
                    }
                    if let Some(batches) = &mut retained {
                        batches.push(batch.clone());
                    }
                    if sender.blocking_send(batch).is_err() {
                        return Ok(());
                    }
                }
                if let Some(batches) = retained {
                    cache.insert(
                        key,
                        Arc::new(CachedPartition {
                            batches: Arc::new(batches),
                            bytes,
                        }),
                    );
                }
                Ok(())
            });
            while let Some(batch) = receiver.recv().await {
                visitor(batch)?;
            }
            decode
                .await
                .map_err(|e| ReaderError::Decode(e.to_string()))??;
        }
        Ok(())
    }
}
