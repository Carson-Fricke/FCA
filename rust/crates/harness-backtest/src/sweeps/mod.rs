//! Deterministic parameter sweeps over backtest configurations.
//!
//! Port of `harness.sweeps`: deduplicated grid and seeded-random parameter
//! spaces with canonical identity ([`spec`]), catalog-based ranking and
//! one-way sensitivity statistics ([`sensitivity`]), and parallel orchestration
//! over an injected launcher port ([`runner`]). Per-run output stays
//! deterministic regardless of launcher scheduling.

pub mod runner;
pub mod sensitivity;
pub mod spec;

pub use runner::{
    sweep_prefix, CatalogRecord, InMemoryRunBlobStore, InMemoryRunCatalog, RecordingLauncher,
    RunBlobStore, RunCatalog, RunStatus, SweepJobLauncher, SweepRunView, SweepRunner, SweepStatus,
    SweepSubmission,
};
pub use sensitivity::{
    level_text, parameter_sensitivity, rank_runs, ParameterLevelMean, ParameterSensitivity,
    RankedRun, RunRecord, SensitivityError, SweepSensitivity,
};
pub use spec::{
    configuration_digest, configuration_json, derive_run_id, SweepError, SweepMode, SweepPoint,
    SweepSpec, SWEEP_VERSION,
};
