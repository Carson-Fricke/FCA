//! Execution telemetry shared by harness callers, independent of workflow phases.

use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionStage {
    Compiling,
    PreparingData,
    ReadingData,
    RunningStudy,
    RunningBacktest,
    WritingArtifacts,
    Interrupted,
}

/// One observed stage/dataset snapshot. Absent counters are unknown, never estimates.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutionProgress {
    pub stage: ExecutionStage,
    pub symbol: Option<String>,
    pub data_type: Option<String>,
    pub start_date: Option<String>,
    pub end_date: Option<String>,
    pub sessions_ready: Option<u64>,
    pub sessions_total: Option<u64>,
    pub sessions_cached: Option<u64>,
    /// True only when this stage/dataset has finished successfully.
    pub completed: bool,
    pub rows: u64,
    pub bytes: u64,
    pub request_timeout_seconds: Option<u64>,
}

impl ExecutionProgress {
    pub fn stage(stage: ExecutionStage) -> Self {
        Self {
            stage,
            symbol: None,
            data_type: None,
            start_date: None,
            end_date: None,
            sessions_ready: None,
            sessions_total: None,
            sessions_cached: None,
            completed: false,
            rows: 0,
            bytes: 0,
            request_timeout_seconds: None,
        }
    }
}

/// A caller-injected telemetry port. Persistence failure stops execution.
pub type ProgressReporter = Arc<dyn Fn(ExecutionProgress) -> Result<(), String> + Send + Sync>;
