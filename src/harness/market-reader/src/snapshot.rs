//! Immutable snapshot identities; access URLs never enter the manifest.

use lake_contracts::{partition_path, AssetClass, BarInterval, DataType, Date};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::ReaderError;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotRequest {
    pub asset_class: String,
    pub data_type: String,
    pub symbol: String,
    pub start_date: String,
    pub end_date: String,
    pub interval: Option<String>,
}

impl SnapshotRequest {
    pub fn validate(&self) -> Result<(AssetClass, DataType), ReaderError> {
        let fail = |message: &str| ReaderError::Snapshot(message.to_string());
        let asset =
            AssetClass::from_value(&self.asset_class).ok_or_else(|| fail("unknown asset class"))?;
        let data =
            DataType::from_value(&self.data_type).ok_or_else(|| fail("unknown data type"))?;
        let start = Date::parse(&self.start_date).ok_or_else(|| fail("invalid start date"))?;
        let end = Date::parse(&self.end_date).ok_or_else(|| fail("invalid end date"))?;
        if start > end || self.symbol.is_empty() || self.symbol.trim().to_uppercase() != self.symbol
        {
            return Err(fail("noncanonical symbol or reversed date window"));
        }
        match (data, self.interval.as_deref()) {
            (DataType::Bars, Some(token)) => {
                BarInterval::parse(token).map_err(|e| fail(&e.to_string()))?;
            }
            (DataType::Bars, None) => return Err(fail("bars requires an interval")),
            (_, Some(_)) => return Err(fail("only bars accepts an interval")),
            (_, None) => {}
        }
        Ok((asset, data))
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Partition {
    pub session_date: String,
    pub object_path: String,
    pub checksum: String,
    pub row_count: u64,
    pub byte_size: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarketDataSnapshot {
    pub snapshot_id: String,
    pub request: SnapshotRequest,
    pub files: Vec<Partition>,
}

impl MarketDataSnapshot {
    pub fn new(request: SnapshotRequest, files: Vec<Partition>) -> Result<Self, ReaderError> {
        let mut snapshot = Self {
            snapshot_id: String::new(),
            request,
            files,
        };
        snapshot.validate_files()?;
        snapshot.snapshot_id = snapshot.digest()?;
        Ok(snapshot)
    }

    pub fn validate(&self) -> Result<(), ReaderError> {
        self.validate_files()?;
        if self.snapshot_id != self.digest()? {
            return Err(ReaderError::Snapshot("manifest digest mismatch".into()));
        }
        Ok(())
    }

    fn validate_files(&self) -> Result<(), ReaderError> {
        let (asset, data) = self.request.validate()?;
        let mut previous: Option<&str> = None;
        for file in &self.files {
            let date = Date::parse(&file.session_date)
                .ok_or_else(|| ReaderError::Snapshot("invalid partition date".into()))?;
            if file.session_date < self.request.start_date
                || file.session_date > self.request.end_date
                || previous.is_some_and(|prior| prior >= file.session_date.as_str())
            {
                return Err(ReaderError::Snapshot(
                    "partitions must be unique and ordered inside the window".into(),
                ));
            }
            let expected = partition_path(
                date,
                asset,
                data,
                self.request.interval.as_deref(),
                &self.request.symbol,
            )
            .map_err(|e| ReaderError::Snapshot(e.to_string()))?;
            if file.object_path != expected
                || file.checksum.len() != 64
                || !file
                    .checksum
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err(ReaderError::Snapshot(
                    "partition requires a canonical path and sha256".into(),
                ));
            }
            previous = Some(&file.session_date);
        }
        Ok(())
    }

    fn digest(&self) -> Result<String, ReaderError> {
        let bytes = serde_json::to_vec(&(&self.request, &self.files))
            .map_err(|e| ReaderError::Snapshot(e.to_string()))?;
        Ok(hex::encode(Sha256::digest(bytes)))
    }
}
