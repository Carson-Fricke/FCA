//! Application capabilities injected by a combined harness host; no network or storage ownership.
use crate::EnsureSpec;
use crate::LakeClientError;
use crate::ReadSpec;
use harness_market_reader::MarketDataSnapshot;
use harness_market_reader::PartitionSource;

/// Admits acquisition and reads its retained identity; never owns caller waiting policy.
#[async_trait::async_trait]
pub trait MarketAcquisition: Send + Sync {
    async fn ensure(&self, spec: &EnsureSpec) -> Result<crate::EnsureResponse, LakeClientError>;
    async fn coverage(&self, spec: &EnsureSpec) -> Result<crate::CoverageReport, LakeClientError>;
    async fn job(&self, id: &str) -> Result<crate::JobStatus, LakeClientError>;
}
/// Supplies immutable snapshot metadata and verified object bytes, without acquisition authority.
#[async_trait::async_trait]
pub trait MarketSnapshots: Send + Sync {
    async fn snapshot(&self, request: &ReadSpec) -> Result<MarketDataSnapshot, LakeClientError>;
    fn source(&self) -> &dyn PartitionSource;
}
/// Supplies point-in-time reference values without job or snapshot mutation.
#[async_trait::async_trait]
pub trait MarketReference: Send + Sync {
    async fn universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError>;
    async fn facts(&self, symbols: &[String], as_of: &str) -> Result<Facts, LakeClientError>;
}
pub type Facts = std::collections::BTreeMap<String, std::collections::BTreeMap<String, String>>;
#[derive(Debug)]
pub enum OptionResolution {
    Resolved(harness_contracts::ResolvedOption),
    Pending { job_id: String },
}
/// Resolves a selector or acknowledges one retained acquisition job.
#[async_trait::async_trait]
pub trait MarketOptions: Send + Sync {
    async fn resolve_option(
        &self,
        selector: &harness_contracts::OptionSelector,
    ) -> Result<OptionResolution, LakeClientError>;
}
/// Host composition only; consumers use the typed capability methods above.
pub trait MarketAccess:
    MarketAcquisition + MarketSnapshots + MarketReference + MarketOptions
{
}
impl<T: MarketAcquisition + MarketSnapshots + MarketReference + MarketOptions> MarketAccess for T {}

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
