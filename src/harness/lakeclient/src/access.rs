//! Application capabilities injected by a combined harness host; no network or storage ownership.
use crate::{EnsureSpec, LakeClientError, ReadSpec};
use harness_market_reader::{MarketDataSnapshot, PartitionSource};
use serde_json::Value;

pub enum MarketRequest {
    Ensure(EnsureSpec),
    Coverage(EnsureSpec),
    Job(String),
    Universe(String),
    Facts { symbols: Vec<String>, as_of: String },
    ResolveOption(harness_contracts::OptionSelector),
}
#[async_trait::async_trait]
pub trait MarketAccess: Send + Sync {
    async fn request(&self, request: MarketRequest) -> Result<Value, LakeClientError>;
    async fn snapshot(&self, request: &ReadSpec) -> Result<MarketDataSnapshot, LakeClientError>;
    fn source(&self) -> &dyn PartitionSource;
}

pub(crate) struct MeasuredSource<'a> {
    pub inner: &'a dyn PartitionSource,
    pub observed: &'a std::sync::atomic::AtomicU64,
}
impl PartitionSource for MeasuredSource<'_> {
    fn read<'a>(
        &'a self,
        partition: &'a harness_market_reader::Partition,
    ) -> futures_util::future::BoxFuture<'a, Result<bytes::Bytes, harness_market_reader::ReaderError>>
    {
        Box::pin(async move {
            let bytes = self.inner.read(partition).await?;
            self.observed
                .fetch_add(bytes.len() as u64, std::sync::atomic::Ordering::Relaxed);
            Ok(bytes)
        })
    }
}
