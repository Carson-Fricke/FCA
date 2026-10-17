//! Option contract metadata in independent atomic local files; coverage uses PublicationCatalog.
use lake_catalog::{ContractRecord, ContractRepository};
use lake_contracts::{canonical_symbol, DataError, Date};
use lake_storage::{BlobStore, LocalFilesystemStore, ReadableBlobStore};
use sha2::{Digest, Sha256};
use std::path::PathBuf;

pub struct FileCatalog {
    objects: LocalFilesystemStore,
}
fn hash(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}
fn io(e: impl std::fmt::Display) -> DataError {
    DataError::Io(e.to_string())
}
impl FileCatalog {
    pub fn new(root: PathBuf) -> Self {
        Self {
            objects: LocalFilesystemStore::new(root),
        }
    }
    fn read<T: serde::de::DeserializeOwned>(&self, key: &str) -> Result<Option<T>, DataError> {
        if !self.objects.root().join(key).is_file() {
            return Ok(None);
        }
        serde_json::from_slice(&self.objects.read(key)?)
            .map(Some)
            .map_err(io)
    }
    fn write(&self, key: &str, value: &impl serde::Serialize) -> Result<(), DataError> {
        self.objects
            .replace(key, &serde_json::to_vec(value).map_err(io)?)
    }
}
impl ContractRepository for FileCatalog {
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError> {
        self.read(&format!(
            "contracts/{}.json",
            hash(&canonical_symbol(symbol)?)
        ))
    }
    fn upsert(&self, record: &ContractRecord) -> Result<(), DataError> {
        self.write(&format!("contracts/{}.json", hash(&record.symbol)), record)
    }
    fn list(
        &self,
        underlying: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ContractRecord>, DataError> {
        let underlying = underlying.map(canonical_symbol).transpose()?;
        let mut rows = Vec::new();
        for path in self.objects.list_prefix("contracts")? {
            if !path.ends_with(".json") {
                continue;
            }
            let record: ContractRecord =
                serde_json::from_slice(&self.objects.read(&path)?).map_err(io)?;
            if underlying.as_ref().is_none_or(|v| v == &record.underlying)
                && as_of.is_none_or(|date| {
                    record.listed_date.is_none_or(|v| date >= v)
                        && record.delisted_date.is_none_or(|v| date <= v)
                })
            {
                rows.push(record);
            }
        }
        rows.sort_by(|a, b| a.symbol.cmp(&b.symbol));
        Ok(rows)
    }
}
