//! Bounded retained Arrow data and checksum-addressed Parquet cache files.

use crate::{CacheConfig, ReaderError};
use arrow_array::RecordBatch;
use bytes::Bytes;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

pub(crate) struct CachedPartition {
    pub batches: Arc<Vec<RecordBatch>>,
    pub bytes: usize,
}

#[derive(Default)]
struct Memory {
    entries: HashMap<String, (Arc<CachedPartition>, u64, usize)>,
    bytes: usize,
    clock: u64,
}

pub(crate) struct PartitionCache {
    pub config: CacheConfig,
    memory: Mutex<Memory>,
    disk: Mutex<bool>,
    flights: Mutex<HashMap<String, std::sync::Weak<tokio::sync::Mutex<()>>>>,
}

impl PartitionCache {
    pub fn new(config: CacheConfig) -> Self {
        Self {
            config,
            memory: Mutex::new(Memory::default()),
            disk: Mutex::new(false),
            flights: Mutex::new(HashMap::new()),
        }
    }

    pub fn flight(&self, key: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut flights = self.flights.lock().expect("partition flights");
        flights.retain(|_, flight| flight.strong_count() > 0);
        if let Some(flight) = flights.get(key).and_then(std::sync::Weak::upgrade) {
            return flight;
        }
        let flight = Arc::new(tokio::sync::Mutex::new(()));
        flights.insert(key.to_string(), Arc::downgrade(&flight));
        flight
    }

    pub fn get(&self, key: &str) -> Option<Arc<CachedPartition>> {
        let mut memory = self.memory.lock().expect("partition cache");
        memory.clock += 1;
        let clock = memory.clock;
        memory.entries.get_mut(key).map(|(entry, used, _)| {
            *used = clock;
            entry.clone()
        })
    }

    pub fn insert(&self, key: String, entry: Arc<CachedPartition>) {
        // Account for retained headers and keys too: zero-row partitions must
        // have positive weight, otherwise their metadata can grow without bound.
        let overhead = 2
            * (std::mem::size_of::<(String, (Arc<CachedPartition>, u64, usize))>()
                + std::mem::size_of::<CachedPartition>()
                + std::mem::size_of::<Vec<RecordBatch>>());
        let weight = entry
            .bytes
            .saturating_add(overhead)
            .saturating_add(key.capacity())
            .saturating_add(
                entry
                    .batches
                    .capacity()
                    .saturating_mul(std::mem::size_of::<RecordBatch>()),
            );
        if self.config.memory_bytes == 0 || weight > self.config.memory_bytes {
            return;
        }
        let mut memory = self.memory.lock().expect("partition cache");
        while memory.bytes.saturating_add(weight) > self.config.memory_bytes {
            let oldest = memory
                .entries
                .iter()
                .min_by_key(|(_, (_, used, _))| *used)
                .map(|(key, _)| key.clone());
            let Some(oldest) = oldest else { break };
            if let Some((_, _, removed_weight)) = memory.entries.remove(&oldest) {
                memory.bytes -= removed_weight;
            }
        }
        memory.clock += 1;
        let used = memory.clock;
        memory.bytes += weight;
        if let Some((_, _, old_weight)) = memory.entries.insert(key, (entry, used, weight)) {
            memory.bytes -= old_weight;
        }
    }

    pub fn read_disk(&self, checksum: &str) -> Result<Option<Bytes>, ReaderError> {
        if self.config.disk_bytes == 0 {
            return Ok(None);
        }
        {
            let mut initialized = self.disk.lock().expect("partition disk cache");
            if !*initialized {
                if self.config.directory.exists() {
                    self.trim_disk()?;
                }
                *initialized = true;
            }
        }
        let path = self.config.directory.join(format!("{checksum}.parquet"));
        match std::fs::read(&path) {
            Ok(bytes) => {
                if let Ok(file) = std::fs::File::open(path) {
                    let _ =
                        file.set_times(std::fs::FileTimes::new().set_modified(SystemTime::now()));
                }
                Ok(Some(bytes.into()))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(ReaderError::Cache(error.to_string())),
        }
    }

    pub fn write_disk(&self, checksum: &str, payload: &Bytes) -> Result<(), ReaderError> {
        if payload.len() as u64 > self.config.disk_bytes || self.config.disk_bytes == 0 {
            return Ok(());
        }
        let mut initialized = self.disk.lock().expect("partition disk cache");
        std::fs::create_dir_all(&self.config.directory)
            .map_err(|e| ReaderError::Cache(e.to_string()))?;
        let path = self.config.directory.join(format!("{checksum}.parquet"));
        if path.exists() {
            if !*initialized {
                self.trim_disk()?;
                *initialized = true;
            }
            return Ok(());
        }
        if !path.exists() {
            let temp = self
                .config
                .directory
                .join(format!("{checksum}.{}.tmp", std::process::id()));
            std::fs::write(&temp, payload).map_err(|e| ReaderError::Cache(e.to_string()))?;
            if let Err(error) = std::fs::rename(&temp, &path) {
                let _ = std::fs::remove_file(&temp);
                if !path.exists() {
                    return Err(ReaderError::Cache(error.to_string()));
                }
            }
        }
        self.trim_disk()?;
        *initialized = true;
        Ok(())
    }

    fn trim_disk(&self) -> Result<(), ReaderError> {
        let mut entries = Vec::new();
        for entry in std::fs::read_dir(&self.config.directory)
            .map_err(|e| ReaderError::Cache(e.to_string()))?
        {
            let entry = entry.map_err(|e| ReaderError::Cache(e.to_string()))?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            let Some(digest) = name.strip_suffix(".parquet") else {
                continue;
            };
            if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
                continue;
            }
            let metadata = entry
                .metadata()
                .map_err(|e| ReaderError::Cache(e.to_string()))?;
            if metadata.is_file() {
                entries.push((
                    metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                    metadata.len(),
                    entry.path(),
                ));
            }
        }
        entries.sort_by_key(|(used, _, _)| *used);
        let mut total: u64 = entries.iter().map(|(_, bytes, _)| *bytes).sum();
        for (_, bytes, entry) in entries {
            if total <= self.config.disk_bytes {
                break;
            }
            if let Err(error) = std::fs::remove_file(entry) {
                if error.kind() != std::io::ErrorKind::NotFound {
                    return Err(ReaderError::Cache(error.to_string()));
                }
            }
            total -= bytes;
        }
        Ok(())
    }
}
