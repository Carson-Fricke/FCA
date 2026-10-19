//! Verified Parquet decoding; sources supply bytes, never provider execution.

use crate::cache::{CachedPartition, PartitionCache};
use crate::{CacheConfig, MarketDataSnapshot, Partition, ReaderError};
use arrow_array::RecordBatch;
use bytes::Bytes;
use futures_util::future::BoxFuture;
use futures_util::{stream, StreamExt};
use lake_contracts::{schema_for, AssetClass, DataType};
use parquet::arrow::arrow_reader::{ParquetRecordBatchReader, ParquetRecordBatchReaderBuilder};
use sha2::{Digest, Sha256};
use std::sync::{Arc, OnceLock};

pub trait PartitionSource: Send + Sync {
    fn read<'a>(&'a self, partition: &'a Partition) -> BoxFuture<'a, Result<Bytes, ReaderError>>;
}

#[derive(Clone)]
pub struct MarketDataReader {
    cache: Arc<PartitionCache>,
    slots: Arc<tokio::sync::Semaphore>,
    prefetch_slots: Arc<tokio::sync::Semaphore>,
}

enum PreparedPartition {
    Cached(Arc<CachedPartition>),
    Streaming {
        reader: ParquetRecordBatchReader,
        prefix: Vec<RecordBatch>,
        slot: tokio::sync::OwnedSemaphorePermit,
        prefetch_slot: Option<tokio::sync::OwnedSemaphorePermit>,
    },
    Skipped,
}

impl std::fmt::Debug for MarketDataReader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MarketDataReader")
            .field("config", &self.cache.config)
            .finish()
    }
}

impl MarketDataReader {
    /// Share verified caches while giving an independently backpressured cursor
    /// its own decode slots. A merge must be able to obtain every source head.
    pub fn cursor(&self) -> Self {
        Self {
            cache: self.cache.clone(),
            slots: Arc::new(tokio::sync::Semaphore::new(1)),
            prefetch_slots: Arc::new(tokio::sync::Semaphore::new(0)),
        }
    }
    pub fn new(config: CacheConfig) -> Self {
        Self {
            cache: Arc::new(PartitionCache::new(config)),
            slots: Arc::new(tokio::sync::Semaphore::new(4)),
            // Leave capacity for a required head read even when other visits
            // retain oversized lookahead partitions awaiting ordered delivery.
            prefetch_slots: Arc::new(tokio::sync::Semaphore::new(3)),
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

    /// Visit ordered batches with bounded lookahead; oversized partitions stream.
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
        let mut preparations = stream::iter(0..snapshot.files.len())
            .map(|index| async move {
                let partition = &snapshot.files[index];
                self.prepare(partition, source, asset, data, index != 0)
                    .await
                    .map(|prepared| (partition, prepared))
            })
            .buffered(2);
        let mut current = preparations.next().await.transpose()?;
        while let Some((partition, prepared)) = current {
            let prepared = match prepared {
                PreparedPartition::Skipped => {
                    self.prepare(partition, source, asset, data, false).await?
                }
                prepared => prepared,
            };
            // Poll the next bounded window while decoding/delivering this head.
            // These are scoped futures: a failed visitor drops pending downloads.
            let (_, next) =
                futures_util::future::try_join(Self::consume(prepared, &mut visitor), async {
                    preparations.next().await.transpose()
                })
                .await?;
            current = next;
        }
        Ok(())
    }

    async fn prepare(
        &self,
        partition: &Partition,
        source: &dyn PartitionSource,
        asset: AssetClass,
        data: DataType,
        speculative: bool,
    ) -> Result<PreparedPartition, ReaderError> {
        let key = format!(
            "{}:{}:{}:{}:{}",
            asset.value(),
            data.value(),
            partition.checksum,
            partition.row_count,
            partition.byte_size
        );
        if let Some(cached) = self.cache.get(&key) {
            return Ok(PreparedPartition::Cached(cached));
        }
        // Partition count alone cannot bound compressed payload memory. Large
        // symbol-days remain required head reads rather than lookahead payloads.
        if speculative && partition.byte_size > 64 * 1024 * 1024 {
            return Ok(PreparedPartition::Skipped);
        }
        let prefetch_slot = if speculative {
            let Ok(slot) = self.prefetch_slots.clone().try_acquire_owned() else {
                return Ok(PreparedPartition::Skipped);
            };
            Some(slot)
        } else {
            None
        };
        let flight = self.cache.flight(&key);
        let _guard = flight.lock().await;
        if let Some(cached) = self.cache.get(&key) {
            return Ok(PreparedPartition::Cached(cached));
        }
        let slot = if speculative {
            let Ok(slot) = self.slots.clone().try_acquire_owned() else {
                return Ok(PreparedPartition::Skipped);
            };
            slot
        } else {
            self.slots
                .clone()
                .acquire_owned()
                .await
                .map_err(|e| ReaderError::Source(e.to_string()))?
        };
        let cache = self.cache.clone();
        let checksum = partition.checksum.clone();
        let (disk, slot, prefetch_slot) = tokio::task::spawn_blocking(move || {
            cache
                .read_disk(&checksum)
                .map(|disk| (disk, slot, prefetch_slot))
        })
        .await
        .map_err(|e| ReaderError::Cache(e.to_string()))??;
        let payload = match disk {
            Some(bytes) => bytes,
            None => source.read(partition).await?,
        };
        let cache = self.cache.clone();
        let partition = partition.clone();
        tokio::task::spawn_blocking(move || {
            let schema = schema_for(asset, data).map_err(|e| ReaderError::Decode(e.to_string()))?;
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
            let mut reader = builder
                .with_batch_size(8192)
                .build()
                .map_err(|e| ReaderError::Decode(e.to_string()))?;
            let mut prefix = Vec::new();
            if cache.config.memory_bytes == 0 {
                return Ok(PreparedPartition::Streaming {
                    reader,
                    prefix,
                    slot,
                    prefetch_slot,
                });
            }
            let mut bytes = 0usize;
            loop {
                let Some(batch) = reader.next() else { break };
                let batch = batch.map_err(|e| ReaderError::Decode(e.to_string()))?;
                bytes = bytes.saturating_add(batch.get_array_memory_size());
                prefix.push(batch);
                // Transfer the partially decoded reader without decoding the
                // prefix twice or retaining the entire oversized partition.
                if bytes > cache.config.memory_bytes / 4 {
                    return Ok(PreparedPartition::Streaming {
                        reader,
                        prefix,
                        slot,
                        prefetch_slot,
                    });
                }
            }
            let cached = Arc::new(CachedPartition {
                batches: Arc::new(prefix),
                bytes,
            });
            cache.insert(key, cached.clone());
            Ok(PreparedPartition::Cached(cached))
        })
        .await
        .map_err(|e| ReaderError::Decode(e.to_string()))?
    }

    async fn consume<F>(prepared: PreparedPartition, visitor: &mut F) -> Result<(), ReaderError>
    where
        F: FnMut(RecordBatch) -> Result<(), ReaderError> + Send,
    {
        let (reader, prefix, slot, prefetch_slot) = match prepared {
            PreparedPartition::Streaming {
                reader,
                prefix,
                slot,
                prefetch_slot,
            } => (reader, prefix, slot, prefetch_slot),
            PreparedPartition::Cached(cached) => {
                for batch in cached.batches.iter() {
                    visitor(batch.clone())?;
                }
                return Ok(());
            }
            PreparedPartition::Skipped => unreachable!("head partition must be prepared"),
        };
        let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
        let decode = tokio::task::spawn_blocking(move || {
            // Blocking work cannot be aborted. Keep its capacity until channel
            // closure stops it, including cancellation or a failed visitor.
            let _slot = slot;
            let _prefetch_slot = prefetch_slot;
            for batch in prefix {
                if sender.blocking_send(batch).is_err() {
                    return Ok(());
                }
            }
            for batch in reader {
                let batch = batch.map_err(|e| ReaderError::Decode(e.to_string()))?;
                if sender.blocking_send(batch).is_err() {
                    return Ok(());
                }
            }
            Ok(())
        });
        while let Some(batch) = receiver.recv().await {
            visitor(batch)?;
        }
        decode
            .await
            .map_err(|e| ReaderError::Decode(e.to_string()))?
    }
}
