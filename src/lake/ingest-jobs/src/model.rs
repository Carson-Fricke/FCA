//! Immutable work and mutable execution state persisted together under one job ID.
use lake_catalog::{JobRecord, JobState};
use lake_contracts::{DataError, DateTime, OptionSelector};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum WorkRequest {
    Dataset {
        asset_class: String,
        data_type: String,
        symbol: String,
        start_date: String,
        end_date: String,
        interval: Option<String>,
    },
    OptionSelection {
        selector: OptionSelector,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DurableJob {
    pub record: JobRecord,
    pub requests: Vec<WorkRequest>,
    pub execution: crate::WorkerSpec,
    pub dispatch_started: bool,
    pub dispatched_at: Option<i64>,
    pub group_deleted: bool,
    pub result: Option<serde_json::Value>,
}
impl DurableJob {
    pub fn new(
        job_id: String,
        requests: Vec<WorkRequest>,
        execution: impl Into<crate::WorkerSpec>,
    ) -> Result<Self, DataError> {
        validate_id(&job_id)?;
        if requests.is_empty() {
            return Err(DataError::value("ingestion work is empty"));
        }
        for (index, request) in requests.iter().enumerate() {
            request.validate()?;
            if requests[..index].contains(request) {
                return Err(DataError::value(
                    "ingestion work contains duplicate requests",
                ));
            }
        }
        let now = now();
        Ok(Self {
            record: JobRecord::new(job_id, JobState::Queued, now, now, None)?,
            requests,
            execution: execution.into(),
            dispatch_started: false,
            dispatched_at: None,
            group_deleted: false,
            result: None,
        })
    }
    pub fn terminal(&self) -> bool {
        matches!(self.record.state, JobState::Complete | JobState::Failed)
    }

    /// Ownership permits lifecycle progress, never mutation of accepted input or terminal outcomes.
    pub fn validate_update(&self, next: &Self) -> Result<(), DataError> {
        if self.requests != next.requests
            || self.execution != next.execution
            || self.record.job_id != next.record.job_id
            || self.record.created_at != next.record.created_at
            || (self.dispatch_started && !next.dispatch_started)
            || (self.dispatched_at.is_some() && self.dispatched_at != next.dispatched_at)
            || (self.group_deleted && !next.group_deleted)
            || (self.terminal() && (self.record != next.record || self.result != next.result))
        {
            return Err(DataError::Conflict(
                "accepted input and terminal ingestion outcomes are immutable".into(),
            ));
        }
        Ok(())
    }
}
impl WorkRequest {
    pub fn validate(&self) -> Result<(), DataError> {
        use lake_contracts::{symbol_filename, AssetClass, BarInterval, DataType, Date};
        match self {
            Self::OptionSelection { selector } => {
                selector.validate()?;
            }
            Self::Dataset {
                asset_class,
                data_type,
                symbol,
                start_date,
                end_date,
                interval,
            } => {
                let asset = AssetClass::from_value(asset_class)
                    .ok_or_else(|| DataError::value("unknown ingestion asset class"))?;
                let data = DataType::from_value(data_type)
                    .ok_or_else(|| DataError::value("unknown ingestion data type"))?;
                if !matches!(
                    asset,
                    AssetClass::Equity | AssetClass::Crypto | AssetClass::Option
                ) || !matches!(data, DataType::Trade | DataType::Quote | DataType::Bars)
                {
                    return Err(DataError::value("unsupported ingestion dataset"));
                }
                if symbol.trim().to_uppercase() != *symbol {
                    return Err(DataError::value("ingestion symbols must be canonical"));
                }
                symbol_filename(symbol)?;
                let start = Date::parse(start_date)
                    .ok_or_else(|| DataError::value("invalid ingestion start date"))?;
                let end = Date::parse(end_date)
                    .ok_or_else(|| DataError::value("invalid ingestion end date"))?;
                if start > end || start.isoformat() != *start_date || end.isoformat() != *end_date {
                    return Err(DataError::value("invalid ingestion date window"));
                }
                match (data, interval.as_deref()) {
                    (DataType::Bars, Some(token)) => {
                        BarInterval::parse(token)?;
                    }
                    (DataType::Bars, None) => {
                        return Err(DataError::value("bars requires an interval"))
                    }
                    (_, Some(_)) => return Err(DataError::value("interval applies only to bars")),
                    _ => {}
                }
            }
        }
        Ok(())
    }
}
pub(crate) fn validate_id(id: &str) -> Result<(), DataError> {
    if id.len() > 128
        || id.is_empty()
        || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err(DataError::value("invalid ingestion job ID"));
    }
    Ok(())
}
pub(crate) fn now() -> DateTime {
    DateTime::from_micros(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_micros()
            .min(i64::MAX as u128) as i64,
    )
}
