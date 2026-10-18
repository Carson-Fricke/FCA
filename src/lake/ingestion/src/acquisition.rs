//! Shared acquisition-to-staging composition over typed provider pages and writer ports.
use crate::{sorted::SortedRuns, PartitionWriter};
use arrow_array::RecordBatch;
use futures_util::StreamExt;
use lake_contracts::{schema_for, AssetClass, DataError, DataType, Date};
use lake_providers::{ProviderError, RecordStream};
use lake_storage::ParquetMetadata;
use std::sync::Arc;

pub(crate) struct StagingSpec {
    pub asset: AssetClass,
    pub data: DataType,
    pub day: Date,
    pub symbol: String,
}

pub(crate) async fn stage_pages<T: Send + 'static>(
    mut pages: RecordStream<'_, T>,
    project: impl Fn(Vec<T>) -> Result<RecordBatch, DataError> + Send + Sync + 'static,
    writer: Arc<dyn PartitionWriter>,
    spec: StagingSpec,
) -> Result<ParquetMetadata, ProviderError> {
    let pipeline = lake_pipeline::Pipeline::global();
    let mut runs = SortedRuns::new(Arc::new(schema_for(spec.asset, spec.data).map_err(error)?));
    let project = Arc::new(project);
    while let Some(page) = pages.next().await {
        let page = page?;
        let project = project.clone();
        let cpu = pipeline.cpu().await.map_err(error)?;
        runs = tokio::task::spawn_blocking(move || {
            let _cpu = cpu;
            let _allocation = page.allocation;
            runs.push(project(page.records)?)?;
            Ok::<_, DataError>(runs)
        })
        .await
        .map_err(error)?
        .map_err(error)?;
    }
    let cpu = pipeline.cpu().await.map_err(error)?;
    tokio::task::spawn_blocking(move || {
        let _cpu = cpu;
        writer.write_batches(
            Box::new(runs.finish()?),
            spec.day,
            spec.asset,
            spec.data,
            None,
            &spec.symbol,
        )
    })
    .await
    .map_err(error)?
    .map_err(error)
}
fn error(error: impl std::fmt::Display) -> ProviderError {
    ProviderError::Configuration(error.to_string())
}
