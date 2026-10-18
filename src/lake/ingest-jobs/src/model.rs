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
    /// Internal partition identity; every partition retains the execution's job_id.
    pub partition: Option<String>,
    pub dispatch_started: bool,
    pub dispatched_at: Option<i64>,
    pub execution_deleted: bool,
    pub result: Option<serde_json::Value>,
    #[serde(deserialize_with = "serde::Deserialize::deserialize")]
    pub metrics: Option<serde_json::Value>,
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
        if requests.len() > 1
            && requests
                .iter()
                .any(|request| matches!(request, WorkRequest::OptionSelection { .. }))
        {
            return Err(DataError::value(
                "option selection is one standalone ingestion operation",
            ));
        }
        for (index, request) in requests.iter().enumerate() {
            request.validate()?;
            if requests[..index].contains(request) {
                return Err(DataError::value(
                    "ingestion work contains duplicate requests",
                ));
            }
            for previous in &requests[..index] {
                if let (
                    WorkRequest::Dataset {
                        asset_class: a,
                        data_type: d,
                        symbol: s,
                        interval: i,
                        start_date: start,
                        end_date: end,
                    },
                    WorkRequest::Dataset {
                        asset_class: pa,
                        data_type: pd,
                        symbol: ps,
                        interval: pi,
                        start_date: pstart,
                        end_date: pend,
                    },
                ) = (request, previous)
                {
                    if (a, d, s, i) == (pa, pd, ps, pi) && start <= pend && pstart <= end {
                        return Err(DataError::value(
                            "ingestion work contains overlapping dataset ranges",
                        ));
                    }
                }
            }
        }
        let now = now();
        Ok(Self {
            record: JobRecord::new(job_id, JobState::Queued, now, now, None)?,
            requests,
            execution: execution.into(),
            partition: None,
            dispatch_started: false,
            dispatched_at: None,
            execution_deleted: false,
            result: None,
            metrics: None,
        })
    }
    pub fn terminal(&self) -> bool {
        matches!(self.record.state, JobState::Complete | JobState::Failed)
    }

    pub fn reference(&self) -> WorkRef {
        WorkRef {
            job_id: self.record.job_id.clone(),
            partition: self.partition.clone(),
        }
    }

    /// Deterministic, disjoint calendar ranges. Reconciliation persists each before dispatch.
    pub fn partitions(&self) -> Result<Vec<Self>, DataError> {
        use lake_contracts::Date;
        use sha2::{Digest, Sha256};
        if self.partition.is_some() {
            return Err(DataError::value("a partition cannot be partitioned again"));
        }
        let max_days = self.execution.partition_days();
        if max_days == 0 {
            return Err(DataError::value("partition days must be positive"));
        }
        let mut work = Vec::new();

        for request in &self.requests {
            let mut parts = Vec::new();
            match request {
                WorkRequest::Dataset {
                    start_date,
                    end_date,
                    ..
                } => {
                    let mut start = Date::parse(start_date)
                        .ok_or_else(|| DataError::value("invalid start date"))?;
                    let last = Date::parse(end_date)
                        .ok_or_else(|| DataError::value("invalid end date"))?;
                    let mut length = 1usize;
                    let mut cursor = start;
                    while cursor < last {
                        length += 1;
                        cursor = cursor.add_days(1);
                    }
                    let days = length
                        .div_ceil(self.execution.max_workers().max(1) * 4)
                        .min(usize::from(max_days))
                        .max(1) as i32;
                    loop {
                        let end = start.add_days(days - 1).min(last);
                        let mut part = request.clone();
                        if let WorkRequest::Dataset {
                            start_date,
                            end_date,
                            ..
                        } = &mut part
                        {
                            *start_date = start.isoformat();
                            *end_date = end.isoformat();
                        }
                        parts.push(part);
                        if end == last {
                            break;
                        }
                        start = end.add_days(1);
                    }
                }
                WorkRequest::OptionSelection { .. } => parts.push(request.clone()),
            }
            work.extend(parts);
        }
        let lane_count = work.len().min(self.execution.max_workers().max(1));
        let mut lanes = vec![Vec::new(); lane_count];
        for (index, request) in work.into_iter().enumerate() {
            lanes[index % lane_count].push(request);
        }
        let mut work = Vec::new();
        for requests in lanes {
            let bytes = serde_json::to_vec(&requests).map_err(DataError::io)?;
            let mut partition = self.clone();
            partition.partition = Some(hex::encode(Sha256::digest(bytes)));
            partition.requests = requests;
            partition.record.state = JobState::Queued;
            partition.record.updated_at = self.record.created_at;
            partition.record.progress = None;
            partition.record.error_message = None;
            partition.dispatch_started = false;
            partition.dispatched_at = None;
            partition.execution_deleted = false;
            partition.result = None;
            partition.metrics = None;
            work.push(partition);
        }
        Ok(work)
    }

    /// Ownership permits lifecycle progress, never mutation of accepted input or terminal outcomes.
    pub fn validate_update(&self, next: &Self) -> Result<(), DataError> {
        if self.requests != next.requests
            || self.partition != next.partition
            || self.execution != next.execution
            || self.record.job_id != next.record.job_id
            || self.record.created_at != next.record.created_at
            || (self.dispatch_started && !next.dispatch_started)
            || (self.dispatched_at.is_some() && self.dispatched_at != next.dispatched_at)
            || (self.execution_deleted && !next.execution_deleted)
            || (self.terminal()
                && (self.record != next.record
                    || self.result != next.result
                    || self.metrics != next.metrics))
        {
            return Err(DataError::Conflict(
                "accepted input and terminal ingestion outcomes are immutable".into(),
            ));
        }
        Ok(())
    }
}

/// A storage/worker address, never an additional execution job ID.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkRef {
    pub job_id: String,
    pub partition: Option<String>,
}
impl WorkRef {
    pub fn root(job_id: &str) -> Self {
        Self {
            job_id: job_id.into(),
            partition: None,
        }
    }
    pub fn path(&self) -> Result<String, DataError> {
        validate_id(&self.job_id)?;
        match &self.partition {
            None => Ok(format!("ingestion/jobs/{}.json", self.job_id)),
            Some(key)
                if key.len() == 64
                    && key
                        .bytes()
                        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()) =>
            {
                Ok(format!("ingestion/partitions/{}/{}.json", self.job_id, key))
            }
            _ => Err(DataError::value("invalid ingestion partition key")),
        }
    }
    pub fn worker_key(&self) -> Result<String, DataError> {
        use sha2::{Digest, Sha256};
        Ok(hex::encode(Sha256::digest(self.path()?.as_bytes())))
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
