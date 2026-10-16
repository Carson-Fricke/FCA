//! JIT compile/load seam for [`harness-strategy-sdk`] authored programs.
//!
//! On submit the platform writes the authored source into a managed cargo
//! workspace with reusable fingerprinted dependency targets and immutable library generations under `target/`, runs `cargo build --release` against
//! the pinned toolchain, and loads the produced `cdylib` through `libloading`
//! behind the existing [`StrategyRuntime`](backtest::backtest::engine_core::StrategyRuntime)
//! engine seam. Identical source, SDK version, and toolchain produce an
//! identical digest, and the compile step is cached by that digest. Studies and
//! strategies share exact subscription-driven quote, trade, and atomic-frame
//! routing. Raw individual bars fail with [`STUDY_BARS_ONLY`].
//!
//! Screening programs do not run through the engine: they are one-shot
//! cross-sectional evaluations, so the host builds one
//! [`ScreenContext`](harness_strategy_sdk::ScreenContext) and calls
//! [`run_screening_source`], which ranks the returned rows host-side.

mod compile;
mod compiler;
mod config;
mod diagnostics;
mod error;
mod load;
mod provenance;
mod run;
mod runtime;
mod screen;
mod screen_run;
mod source;
mod toolchain;
mod validation;
mod workspace;

pub use compile::{
    compile, validation_service, CompiledArtifact, ProgramKind, ValidationFailure,
    ValidationService,
};
pub use config::{build_instances, program_kind, JIT_IMPLEMENTATION, SOURCE_PARAMETER};
pub use error::JitError;
pub use load::{LoadedScreening, LoadedStrategy, LoadedStudy};
pub use run::{
    configuration_subscriptions, inspect_configuration, run_backtest_configuration,
    run_study_configuration, validate_configuration, ConfigurationRunError,
};
pub use runtime::{
    strategy_instance, study_instance, JitStrategyRuntime, JitStudyRuntime, STUDY_BARS_ONLY,
};
pub use screen::{rank_rows, run_screening_source};
pub use screen_run::{
    run_screening_configuration, ScreenOutcome, ScreenRunError, ScreenRunOptions, ScreenStatus,
    SCREEN_ARTIFACT, SCREEN_SCHEMA,
};
pub use source::{
    LakeScreeningSource, ScreeningDataSource, ScreeningDataSourceError, ScreeningLakeReads,
};
pub use toolchain::{resolve_toolchain, Toolchain};
pub use workspace::{ManagedWorkspace, WorkspaceManifest};
