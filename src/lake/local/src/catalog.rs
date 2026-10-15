//! Partition-indexed atomic catalog rows; independent worker publications never rewrite a whole catalog.
use lake_catalog::{
    CatalogRepository, ContractRecord, ContractRepository, CoverageQuery, CoverageRecord,
};
use lake_contracts::{canonical_symbol, AssetClass, DataError, DataType, Date};
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
    fn coverage_key(
        asset: AssetClass,
        data: DataType,
        interval: Option<&str>,
        symbol: &str,
        date: Date,
    ) -> Result<String, DataError> {
        Ok(format!(
            "coverage/{}/{}/{}/{}/{}.json",
            asset.value(),
            data.value(),
            hash(&canonical_symbol(symbol)?),
            hash(interval.unwrap_or("")),
            date.isoformat()
        ))
    }
}
impl CatalogRepository for FileCatalog {
    fn get(
        &self,
        a: AssetClass,
        d: DataType,
        i: Option<&str>,
        s: &str,
        date: Date,
    ) -> Result<Option<CoverageRecord>, DataError> {
        self.read(&Self::coverage_key(a, d, i, s, date)?)
    }
    fn upsert(&self, r: &CoverageRecord) -> Result<(), DataError> {
        self.write(
            &Self::coverage_key(
                r.asset_class,
                r.data_type,
                r.interval.as_deref(),
                &r.symbol,
                r.session_date,
            )?,
            r,
        )
    }
    fn list(&self, q: &CoverageQuery) -> Result<Vec<CoverageRecord>, DataError> {
        if q.start.zip(q.end).is_some_and(|(a, b)| a > b) {
            return Err(DataError::value("start must be on or before end"));
        }
        let symbol = q.symbol.as_deref().map(canonical_symbol).transpose()?;
        let mut prefix = "coverage".to_string();
        if let Some(a) = q.asset_class {
            prefix.push_str(&format!("/{}", a.value()));
            if let Some(d) = q.data_type {
                prefix.push_str(&format!("/{}", d.value()));
                if let Some(s) = &symbol {
                    prefix.push_str(&format!("/{}", hash(s)));
                    if let Some(i) = &q.interval {
                        prefix.push_str(&format!("/{}", hash(i)));
                    }
                }
            }
        }
        let mut rows = Vec::new();
        for path in self.objects.list_prefix(&prefix)? {
            if !path.ends_with(".json") {
                continue;
            }
            let row: CoverageRecord =
                serde_json::from_slice(&self.objects.read(&path)?).map_err(io)?;
            if q.asset_class.is_none_or(|v| v == row.asset_class)
                && q.data_type.is_none_or(|v| v == row.data_type)
                && symbol.as_ref().is_none_or(|v| v == &row.symbol)
                && q.interval
                    .as_ref()
                    .is_none_or(|v| Some(v) == row.interval.as_ref())
                && q.start.is_none_or(|v| v <= row.session_date)
                && q.end.is_none_or(|v| v >= row.session_date)
            {
                rows.push(row);
            }
        }
        rows.sort_by_key(|r| {
            (
                r.asset_class,
                r.data_type,
                r.symbol.clone(),
                r.interval.clone(),
                r.session_date,
            )
        });
        Ok(rows)
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
