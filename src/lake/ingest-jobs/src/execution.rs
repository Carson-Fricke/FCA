//! Current deployment specifications, independent of admission and reconciliation.
use crate::AcaConfig;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(
    tag = "executor",
    content = "configuration",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum WorkerSpec {
    Aca(AcaConfig),
    Local(LocalConfig),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalConfig {
    pub executable: std::path::PathBuf,
    pub provider_env: Option<std::path::PathBuf>,
    pub root: std::path::PathBuf,
    pub max_workers: usize,
    pub timeout_seconds: u64,
    pub partition_days: u16,
    pub session_concurrency: usize,
}

impl WorkerSpec {
    pub fn max_workers(&self) -> usize {
        match self {
            Self::Aca(v) => v.max_workers,
            Self::Local(v) => v.max_workers,
        }
    }
    pub fn timeout_seconds(&self) -> u64 {
        match self {
            Self::Aca(v) => v.timeout_seconds,
            Self::Local(v) => v.timeout_seconds,
        }
    }
    pub fn partition_days(&self) -> u16 {
        match self {
            Self::Aca(v) => v.partition_days,
            Self::Local(v) => v.partition_days,
        }
    }
}
impl From<AcaConfig> for WorkerSpec {
    fn from(value: AcaConfig) -> Self {
        Self::Aca(value)
    }
}
impl From<LocalConfig> for WorkerSpec {
    fn from(value: LocalConfig) -> Self {
        Self::Local(value)
    }
}

#[async_trait::async_trait]
impl crate::WorkerRunner for crate::AcaRunner {
    fn begin_reconcile(&self) {
        self.begin_reconcile();
    }
    fn configuration(&self) -> WorkerSpec {
        self.config.clone().into()
    }
    fn for_work(&self, spec: &WorkerSpec) -> Result<Self, lake_contracts::DataError> {
        match spec {
            WorkerSpec::Aca(config) => Ok(self.for_execution(config.clone())),
            _ => Err(lake_contracts::DataError::value(
                "ACA runner cannot execute local work",
            )),
        }
    }
    async fn inspect(
        &self,
        work: &crate::WorkRef,
    ) -> Result<crate::ExecutionState, lake_contracts::DataError> {
        self.inspect(work).await
    }
    async fn start(&self, work: &crate::WorkRef) -> Result<(), lake_contracts::DataError> {
        self.start(work).await
    }
    async fn delete(&self, work: &crate::WorkRef) -> Result<(), lake_contracts::DataError> {
        self.delete(work).await
    }
}
