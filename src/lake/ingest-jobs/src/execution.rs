//! Current deployment specifications, independent of admission and reconciliation.
use crate::AciConfig;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(
    tag = "executor",
    content = "configuration",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum WorkerSpec {
    Aci(AciConfig),
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
}

impl WorkerSpec {
    pub fn max_workers(&self) -> usize {
        match self {
            Self::Aci(v) => v.max_workers,
            Self::Local(v) => v.max_workers,
        }
    }
    pub fn timeout_seconds(&self) -> u64 {
        match self {
            Self::Aci(v) => v.timeout_seconds,
            Self::Local(v) => v.timeout_seconds,
        }
    }
}
impl From<AciConfig> for WorkerSpec {
    fn from(value: AciConfig) -> Self {
        Self::Aci(value)
    }
}
impl From<LocalConfig> for WorkerSpec {
    fn from(value: LocalConfig) -> Self {
        Self::Local(value)
    }
}

#[async_trait::async_trait]
impl crate::WorkerRunner for crate::AciRunner {
    fn configuration(&self) -> WorkerSpec {
        self.config.clone().into()
    }
    fn for_work(&self, spec: &WorkerSpec) -> Result<Self, lake_contracts::DataError> {
        match spec {
            WorkerSpec::Aci(config) => Ok(self.for_execution(config.clone())),
            _ => Err(lake_contracts::DataError::value(
                "ACI runner cannot execute local work",
            )),
        }
    }
    async fn inspect(&self, id: &str) -> Result<crate::GroupState, lake_contracts::DataError> {
        self.inspect(id).await
    }
    async fn start(&self, id: &str) -> Result<(), lake_contracts::DataError> {
        self.start(id).await
    }
    async fn delete(&self, id: &str) -> Result<(), lake_contracts::DataError> {
        self.delete(id).await
    }
}
