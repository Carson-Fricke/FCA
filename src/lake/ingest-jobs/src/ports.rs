//! Execution and durable ownership ports shared by local processes and Azure containers.
use crate::{DurableJob, GroupState, WorkerSpec};
use lake_contracts::DataError;

#[async_trait::async_trait]
pub trait WorkerRunner: Clone + Send + Sync + 'static {
    fn configuration(&self) -> WorkerSpec;
    fn for_work(&self, specification: &WorkerSpec) -> Result<Self, DataError>;
    async fn inspect(&self, id: &str) -> Result<GroupState, DataError>;
    async fn start(&self, id: &str) -> Result<(), DataError>;
    async fn delete(&self, id: &str) -> Result<(), DataError>;
}

#[async_trait::async_trait]
pub trait JobOwnership: Send + Sync {
    async fn renew(&self) -> Result<(), DataError>;
    async fn release(&self) -> Result<(), DataError>;
    async fn save(&self, job: &DurableJob) -> Result<(), DataError>;
}

#[derive(Clone)]
pub struct ActiveJob {
    pub id: String,
    pub dispatched: bool,
    pub created: i64,
}

#[async_trait::async_trait]
pub trait JobPersistence: Clone + Send + Sync + 'static {
    type Owner: JobOwnership + 'static;
    async fn admit(&self, job: DurableJob) -> Result<DurableJob, DataError>;
    async fn get(&self, id: &str) -> Result<DurableJob, DataError>;
    async fn own(&self, id: &str) -> Result<Self::Owner, DataError>;
    async fn dispatch_lock(&self) -> Result<Self::Owner, DataError>;
    async fn active_jobs(&self) -> Result<Vec<ActiveJob>, DataError>;
}

pub async fn while_owned<T>(
    owner: &impl JobOwnership,
    work: impl std::future::Future<Output = Result<T, DataError>>,
) -> Result<T, DataError> {
    tokio::pin!(work);
    let mut heartbeat = tokio::time::interval_at(
        tokio::time::Instant::now() + std::time::Duration::from_secs(15),
        std::time::Duration::from_secs(15),
    );
    loop {
        tokio::select! {
            biased;
            _ = heartbeat.tick() => owner.renew().await?,
            result = &mut work => return result,
        }
    }
}
