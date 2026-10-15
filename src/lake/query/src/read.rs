//! Snapshot planning shared by native and remote readers.
//!
//! The route validates its query parameters at the HTTP boundary, then plans
//! through the same coverage classification the file query uses: an entry is a
//! readable file only when its state is `complete` and its object path is the
//! canonical `date=/asset_class=/data_type=[interval=]/SYMBOL.parquet`
//! partition. Empty, delisted, missing, partial, and foreign-path entries are
//! never read. The plan keeps only catalog metadata; the shared reader loads
//! verified objects through its injected byte source.

use lake_contracts::{AssetClass, DataType, Date};
use lake_coverage::{EnsureSpec, SessionCoverage};

use crate::application::{classify_sessions, coverage_report, missing_object_path};
use crate::error::QueryApplicationError;
use crate::models::FileQueryContext;

/// A validated read request over one symbol and inclusive session-date window.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadRequest {
    /// Asset class of the requested partitions.
    pub asset_class: AssetClass,
    /// Data type of the requested partitions.
    pub data_type: DataType,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Canonical bars interval token; required for bars and absent otherwise.
    pub interval: Option<String>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
}

/// One ensured terminal partition the read stream serves.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadFile {
    /// Session the partition covers.
    pub session_date: Date,
    /// Canonical Hive object path.
    pub object_path: String,
    pub checksum: Option<String>,
    pub row_count: u64,
    pub byte_size: u64,
}

/// The ordered files one read stream serves, in session order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadPlan {
    /// Asset class of the stream.
    pub asset_class: AssetClass,
    /// Data type of the stream.
    pub data_type: DataType,
    /// Requested canonical symbol.
    pub symbol: String,
    /// Canonical bars interval token, when the stream is bars.
    pub interval: Option<String>,
    /// Ensured terminal files in session order.
    pub files: Vec<ReadFile>,
}

/// Classify coverage and return the ensured files a read stream may serve.
///
/// The report uses the same `"query"` operation the file query uses, so
/// incomplete sessions are simply not served; the caller decides whether an
/// empty plan is `no_data`.
pub fn plan_read(
    context: &FileQueryContext<'_>,
    request: &ReadRequest,
) -> Result<ReadPlan, QueryApplicationError> {
    let spec = EnsureSpec {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: request.symbol.clone(),
        start_date: request.start_date,
        end_date: request.end_date,
        interval: request.interval.clone(),
    };
    let report = coverage_report(context, &spec)?;
    let (usable, _incomplete) = classify_sessions(
        &report,
        request.asset_class,
        request.data_type,
        request.interval.as_deref(),
        &request.symbol,
    );
    plan_from_entries(
        request.asset_class,
        request.data_type,
        request.symbol.clone(),
        request.interval.clone(),
        &usable,
    )
}

/// Build a plan from pre-classified entries (used by the option selection).
pub fn plan_from_entries(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: impl Into<String>,
    interval: Option<String>,
    entries: &[SessionCoverage],
) -> Result<ReadPlan, QueryApplicationError> {
    let mut files = Vec::with_capacity(entries.len());
    for entry in entries {
        let Some(record) = entry.record.as_ref() else {
            return Err(missing_object_path());
        };
        let Some(object_path) = record.object_path.as_deref() else {
            return Err(missing_object_path());
        };
        files.push(ReadFile {
            session_date: entry.session_date(),
            object_path: object_path.to_string(),
            checksum: record.checksum.clone(),
            row_count: record.row_count,
            byte_size: record.byte_size,
        });
    }
    Ok(ReadPlan {
        asset_class,
        data_type,
        symbol: symbol.into(),
        interval,
        files,
    })
}

/// Plan current equity, crypto and option manifests through one selection policy.
pub fn plan_snapshot(
    context: &FileQueryContext<'_>,
    contracts: Option<&dyn lake_catalog::ContractRepository>,
    front_sets: Option<&dyn lake_catalog::DailyFrontSetRepository>,
    request: &ReadRequest,
) -> Result<ReadPlan, QueryApplicationError> {
    if request.asset_class != AssetClass::Option {
        return plan_read(context, request);
    }
    let like = lake_coverage::OptionRequestLike {
        data_type: request.data_type,
        interval: request.interval.as_deref(),
        symbol: Some(&request.symbol),
        underlying: None,
        contract_symbols: None,
        start_date: request.start_date,
        end_date: request.end_date,
    };
    let (report, _) = lake_coverage::option_coverage(
        context.catalog,
        &like,
        Some(lake_coverage::contracts_or_default(contracts)),
        front_sets,
        context.as_of,
    )
    .map_err(|e| QueryApplicationError::new(e.code, e.message, e.details))?;
    let (usable, _) = lake_coverage::classify_option(&report, request.data_type);
    plan_from_entries(
        AssetClass::Option,
        request.data_type,
        &request.symbol,
        request.interval.clone(),
        &usable,
    )
}
