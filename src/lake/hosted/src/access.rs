//! Native harness adapter over catalog, acquisition and object ports. No HTTP calls.
use harness_lakeclient::{EnsureSpec, LakeClientError, MarketAccess, MarketRequest, ReadSpec};
use harness_market_reader::{
    MarketDataSnapshot, Partition, PartitionSource, ReaderError, SnapshotRequest,
};
use lake_catalog::{CatalogRepository, JobRepository};
use lake_contracts::{AssetClass, DataType, Date};
use lake_coverage::{EnsureAdmission, EnsureOutcome};
use serde_json::{json, Value};
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
    Ok(lake_coverage::EnsureSpec {
        asset_class: AssetClass::from_value(&value.asset_class)
            .ok_or_else(|| error("unknown asset class"))?,
        data_type: DataType::from_value(&value.data_type)
            .ok_or_else(|| error("unknown data type"))?,
        symbol: value.symbol.clone(),
        start_date: date(&value.start_date)?,
        end_date: date(&value.end_date)?,
        interval: value.interval.clone(),
    })
}
fn date(value: &str) -> Result<Date, LakeClientError> {
    Date::parse(value).ok_or_else(|| error("invalid date"))
}
#[async_trait::async_trait]
impl MarketAccess for NativeMarketAccess {
    async fn request(&self, request: MarketRequest) -> Result<Value, LakeClientError> {
        if let MarketRequest::ResolveOption(selector) = request {
            let selector = serde_json::from_value(serde_json::to_value(selector).map_err(error)?)
                .map_err(error)?;
            return match (self.options)(selector, None).await {
                Ok(value) => serde_json::to_value(value).map_err(error),
                Err(lake_contracts::DataError::PendingJob(id)) => Ok(json!({"job_id":id})),
                Err(e) => Err(error(e)),
            };
        }
        let as_of = (self.now)();
        let catalog = self.catalog.clone();
        let jobs = self.jobs.clone();
        let admission = self.admission.clone();
        let objects = self.source.0.clone();
        let runtime = tokio::runtime::Handle::current();
        tokio::task::spawn_blocking(move || match request {
            MarketRequest::Ensure(value)=>{
                let spec=spec(&value)?;
                let outcome=runtime.block_on(lake_coverage::ensure_coverage(catalog.as_ref(),&jobs,None,admission,&spec,as_of,None)).map_err(error)?;
                match outcome {
                    EnsureOutcome::Covered(report)=>serde_json::to_value(lake_http::models::CatalogCoverageResponse::from_report(&report,spec.data_type,&spec.symbol)).map_err(error),
                    EnsureOutcome::Accepted(job)=>Ok(json!({"job_id":job.job_id})),
                }
            }
            MarketRequest::Coverage(value)=>{
                let spec=spec(&value)?;
                let report=lake_coverage::requested_coverage(catalog.as_ref(),None,&spec,as_of,"coverage").map_err(error)?;
                serde_json::to_value(lake_http::models::CatalogCoverageResponse::from_report(&report,spec.data_type,&spec.symbol)).map_err(error)
            }
            MarketRequest::Job(id)=>{
                let record=jobs.get(&id).map_err(error)?.ok_or_else(||error("job not found"))?;
                Ok(json!({"job_id":record.job_id,"state":record.state.value(),"error_message":record.error_message}))
            }
            MarketRequest::Universe(as_of)=>Ok(json!({"symbols":lake_query::universe_symbols(objects.as_ref(),date(&as_of)?).map_err(error)?})),
            MarketRequest::Facts{symbols,as_of}=>Ok(json!({"facts":lake_query::symbol_facts(objects.as_ref(),date(&as_of)?,&symbols).map_err(error)?})),
            MarketRequest::ResolveOption(_)=>unreachable!(),
        }).await.map_err(error)?
    }
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
