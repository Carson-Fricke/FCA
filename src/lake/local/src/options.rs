//! Durable local option contract metadata; shared ingestion owns ensure execution.
use crate::LocalLayout;
use lake_catalog::{ContractRecord, ContractRepository};
use lake_contracts::{DataError, Date};
use lake_storage::{BlobStore, LocalFilesystemStore, ReadableBlobStore};
use std::{collections::BTreeMap, sync::Mutex};

pub(crate) struct LocalOptionContracts {
    store: LocalFilesystemStore,
    records: Mutex<BTreeMap<String, ContractRecord>>,
}

impl LocalOptionContracts {
    pub fn open(layout: &LocalLayout) -> Result<Self, DataError> {
        let store = LocalFilesystemStore::new(layout.state_dir());
        let records: BTreeMap<String, ContractRecord> =
            if layout.state_dir().join("option-contracts.json").exists() {
                serde_json::from_slice(&store.read("option-contracts.json")?)
                    .map_err(|e| DataError::value(e.to_string()))?
            } else {
                BTreeMap::new()
            };
        Ok(Self {
            store,
            records: Mutex::new(records),
        })
    }
}

impl ContractRepository for LocalOptionContracts {
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError> {
        Ok(self
            .records
            .lock()
            .map_err(|_| DataError::Io("contract lock poisoned".into()))?
            .get(symbol)
            .cloned())
    }
    fn upsert(&self, record: &ContractRecord) -> Result<(), DataError> {
        let mut records = self
            .records
            .lock()
            .map_err(|_| DataError::Io("contract lock poisoned".into()))?;
        let mut next = records.clone();
        next.insert(record.symbol.clone(), record.clone());
        let bytes = serde_json::to_vec(&next).map_err(|e| DataError::value(e.to_string()))?;
        self.store.replace("option-contracts.json", &bytes)?;
        *records = next;
        Ok(())
    }
    fn list(
        &self,
        underlying: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ContractRecord>, DataError> {
        Ok(self
            .records
            .lock()
            .map_err(|_| DataError::Io("contract lock poisoned".into()))?
            .values()
            .filter(|c| {
                underlying.is_none_or(|u| c.underlying == u)
                    && as_of.is_none_or(|d| {
                        c.listed_date.is_none_or(|l| l <= d)
                            && c.delisted_date.is_none_or(|l| l >= d)
                    })
            })
            .cloned()
            .collect())
    }
}
