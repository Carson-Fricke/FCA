//! Native harness adapter over catalog, acquisition and object ports. No HTTP calls.
use harness_lakeclient::{
    CoverageEntry, CoverageReport, EnsureResponse, EnsureSpec, Facts, JobStatus, LakeClientError,
    MarketAcquisition, MarketOptions, MarketReference, MarketSnapshots, OptionResolution, ReadSpec,
};
use harness_market_reader::{
    MarketDataSnapshot, Partition, PartitionSource, ReaderError, SnapshotRequest,
};
use lake_catalog::{CatalogRepository, JobRepository};
use lake_contracts::{AssetClass, DataType, Date};
use lake_coverage::{EnsureAdmission, EnsureOutcome};
use std::sync::Arc;

pub struct NativeMarketAccess {
    pub catalog: Arc<dyn CatalogRepository>,
    pub jobs: Arc<dyn JobRepository>,
    pub contracts: Arc<dyn lake_catalog::ContractRepository>,
    pub admission: EnsureAdmission,
    pub options: lake_http::state::OptionResolver,
    pub source: StoredObjects,
    pub now: Arc<dyn Fn() -> lake_contracts::DateTime + Send + Sync>,
}
pub struct StoredObjects(pub Arc<dyn lake_storage::ReadableBlobStore>);
fn error(e: impl std::fmt::Display) -> LakeClientError {
    LakeClientError::Protocol(e.to_string())
}
fn spec(value: &EnsureSpec) -> Result<lake_coverage::EnsureSpec, LakeClientError> {
    let request = lake_coverage::EnsureSpec {
        asset_class: AssetClass::from_value(&value.asset_class)
            .ok_or_else(|| error("unknown asset class"))?,
        data_type: DataType::from_value(&value.data_type)
            .ok_or_else(|| error("unknown data type"))?,
        symbol: value.symbol.clone(),
        start_date: date(&value.start_date)?,
        end_date: date(&value.end_date)?,
        interval: value.interval.clone(),
    };
    request.validate_interval().map_err(error)?;
    Ok(request)
}
fn date(value: &str) -> Result<Date, LakeClientError> {
    Date::parse(value).ok_or_else(|| error("invalid date"))
}
#[async_trait::async_trait]
impl MarketAcquisition for NativeMarketAccess {
    async fn ensure(&self, value: &EnsureSpec) -> Result<EnsureResponse, LakeClientError> {
        let spec = spec(value)?;
        let as_of = (self.now)();
        let catalog = self.catalog.clone();
        let jobs = self.jobs.clone();
        let admission = self.admission.clone();
        let contracts = self.contracts.clone();
        let runtime = tokio::runtime::Handle::current();
        tokio::task::spawn_blocking(move || {
            let option_id = if spec.asset_class == AssetClass::Option {
                let like = option_request(&spec);
                let (_, selection) = lake_coverage::option_coverage(
                    catalog.as_ref(),
                    &like,
                    Some(contracts.as_ref()),
                    None,
                    as_of,
                )
                .map_err(error)?;
                Some(lake_coverage::option_ensure_job_id(
                    &like,
                    &selection.symbols(),
                ))
            } else {
                None
            };
            match runtime
                .block_on(lake_coverage::ensure_coverage(
                    catalog.as_ref(),
                    &jobs,
                    None,
                    admission,
                    &spec,
                    as_of,
                    option_id.as_deref(),
                ))
                .map_err(error)?
            {
                EnsureOutcome::Covered(report) => Ok(EnsureResponse {
                    entries: Some(coverage_model(&report, spec.data_type, &spec.symbol).entries),
                    job_id: None,
                }),
                EnsureOutcome::Accepted(job) => Ok(EnsureResponse {
                    entries: None,
                    job_id: Some(job.job_id),
                }),
            }
        })
        .await
        .map_err(error)?
    }
    async fn coverage(&self, value: &EnsureSpec) -> Result<CoverageReport, LakeClientError> {
        let spec = spec(value)?;
        let as_of = (self.now)();
        let catalog = self.catalog.clone();
        let contracts = self.contracts.clone();
        tokio::task::spawn_blocking(move || {
            let report = if spec.asset_class == AssetClass::Option {
                lake_coverage::option_coverage(
                    catalog.as_ref(),
                    &option_request(&spec),
                    Some(contracts.as_ref()),
                    None,
                    as_of,
                )
                .map_err(error)?
                .0
            } else {
                lake_coverage::requested_coverage(catalog.as_ref(), None, &spec, as_of, "coverage")
                    .map_err(error)?
            };
            Ok(coverage_model(&report, spec.data_type, &spec.symbol))
        })
        .await
        .map_err(error)?
    }
    async fn job(&self, id: &str) -> Result<JobStatus, LakeClientError> {
        let jobs = self.jobs.clone();
        let id = id.to_string();
        tokio::task::spawn_blocking(move || {
            let record = jobs
                .get(&id)
                .map_err(error)?
                .ok_or_else(|| error("job not found"))?;
            Ok(JobStatus {
                job_id: record.job_id,
                state: record.state.value().into(),
                error_message: record.error_message,
            })
        })
        .await
        .map_err(error)?
    }
}
#[async_trait::async_trait]
impl MarketReference for NativeMarketAccess {
    async fn universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError> {
        let date = date(as_of)?;
        let objects = self.source.0.clone();
        tokio::task::spawn_blocking(move || {
            lake_query::universe_symbols(objects.as_ref(), date).map_err(error)
        })
        .await
        .map_err(error)?
    }
    async fn facts(&self, symbols: &[String], as_of: &str) -> Result<Facts, LakeClientError> {
        let date = date(as_of)?;
        let symbols = symbols.to_vec();
        let objects = self.source.0.clone();
        tokio::task::spawn_blocking(move || {
            lake_query::symbol_facts(objects.as_ref(), date, &symbols).map_err(error)
        })
        .await
        .map_err(error)?
    }
}
#[async_trait::async_trait]
impl MarketOptions for NativeMarketAccess {
    async fn resolve_option(
        &self,
        selector: &lake_contracts::OptionSelector,
    ) -> Result<OptionResolution, LakeClientError> {
        match (self.options)(selector.clone(), None).await {
            Ok(value) => Ok(OptionResolution::Resolved(value)),
            Err(lake_contracts::DataError::PendingJob(job_id)) => {
                Ok(OptionResolution::Pending { job_id })
            }
            Err(cause) => Err(error(cause)),
        }
    }
}
fn coverage_model(
    report: &lake_coverage::CoverageReport,
    data_type: DataType,
    symbol: &str,
) -> CoverageReport {
    CoverageReport {
        asset_class: report.asset_class.value().into(),
        data_type: data_type.value().into(),
        symbol: symbol.trim().to_uppercase(),
        start_date: report.start_date.isoformat(),
        end_date: report.end_date.isoformat(),
        entries: report
            .entries
            .iter()
            .map(|entry| CoverageEntry {
                session_date: entry.session_date().isoformat(),
                status: entry.state.value().into(),
                is_partial: entry.session.is_partial,
                symbol: None,
                data_type: None,
                row_count: entry.record.as_ref().map(|record| record.row_count),
                error_message: entry
                    .record
                    .as_ref()
                    .and_then(|record| record.error_message.clone()),
            })
            .collect(),
    }
}
#[async_trait::async_trait]
impl MarketSnapshots for NativeMarketAccess {
    async fn snapshot(&self, value: &ReadSpec) -> Result<MarketDataSnapshot, LakeClientError> {
        let as_of = (self.now)();
        let catalog = self.catalog.clone();
        let value = value.clone();
        let contracts = self.contracts.clone();
        tokio::task::spawn_blocking(move || {
            let request = lake_query::ReadRequest {
                asset_class: AssetClass::from_value(&value.asset_class)
                    .ok_or_else(|| error("unknown asset class"))?,
                data_type: DataType::from_value(&value.data_type)
                    .ok_or_else(|| error("unknown data type"))?,
                symbol: value.symbol.trim().to_uppercase(),
                interval: value.interval.clone(),
                start_date: date(&value.start_date)?,
                end_date: date(&value.end_date)?,
            };
            let context = lake_query::FileQueryContext {
                catalog: catalog.as_ref(),
                listings: None,
                sas_issuer: None,
                ensure_callback: None,
                as_of,
                bounded_query: None,
                force_authorized: false,
            };
            let plan =
                lake_query::plan_snapshot(&context, Some(contracts.as_ref()), None, &request)
                    .map_err(error)?;
            let files = plan
                .files
                .into_iter()
                .map(|v| {
                    Ok(Partition {
                        session_date: v.session_date.isoformat(),
                        object_path: v.object_path,
                        checksum: v
                            .checksum
                            .ok_or_else(|| error("partition checksum required"))?,
                        row_count: v.row_count,
                        byte_size: v.byte_size,
                    })
                })
                .collect::<Result<Vec<_>, LakeClientError>>()?;
            MarketDataSnapshot::new(
                SnapshotRequest {
                    asset_class: value.asset_class,
                    data_type: value.data_type,
                    symbol: request.symbol,
                    start_date: value.start_date,
                    end_date: value.end_date,
                    interval: value.interval,
                },
                files,
            )
            .map_err(error)
        })
        .await
        .map_err(error)?
    }
    fn source(&self) -> &dyn PartitionSource {
        &self.source
    }
}
impl PartitionSource for StoredObjects {
    fn read<'a>(
        &'a self,
        partition: &'a Partition,
    ) -> futures_util::future::BoxFuture<'a, Result<bytes::Bytes, ReaderError>> {
        let store = self.0.clone();
        let path = partition.object_path.clone();
        Box::pin(async move {
            tokio::task::spawn_blocking(move || {
                store
                    .read(&path)
                    .map(bytes::Bytes::from)
                    .map_err(|e| ReaderError::Source(e.to_string()))
            })
            .await
            .map_err(|e| ReaderError::Source(e.to_string()))?
        })
    }
}

fn option_request(spec: &lake_coverage::EnsureSpec) -> lake_coverage::OptionRequestLike<'_> {
    lake_coverage::OptionRequestLike {
        data_type: spec.data_type,
        interval: spec.interval.as_deref(),
        symbol: Some(&spec.symbol),
        underlying: None,
        contract_symbols: None,
        start_date: spec.start_date,
        end_date: spec.end_date,
    }
}
