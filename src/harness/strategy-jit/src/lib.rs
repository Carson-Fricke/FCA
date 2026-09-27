//! JIT compile/load seam for [`harness-strategy-sdk`] authored programs.
//!
//! On submit the platform writes the authored source into a managed cargo
//! workspace with one shared `target/`, runs `cargo build --release` against
//! the pinned toolchain, and loads the produced `cdylib` through `libloading`
//! behind the existing [`StrategyRuntime`](backtest::backtest::engine_core::StrategyRuntime)
//! engine seam. Identical source, SDK version, and toolchain produce an
//! identical digest, and the compile step is cached by that digest.

mod compile;
mod config;
mod error;
mod load;
mod run;
mod runtime;
mod toolchain;
mod workspace;

pub use compile::{compile, CompiledArtifact, ProgramKind};
pub use config::{build_instances, program_kind, JIT_IMPLEMENTATION, SOURCE_PARAMETER};
pub use error::JitError;
pub use load::{LoadedStrategy, LoadedStudy};
pub use run::{run_backtest_configuration, run_study_configuration, ConfigurationRunError};
pub use runtime::{strategy_instance, study_instance, JitStrategyRuntime, JitStudyRuntime};
pub use toolchain::{resolve_toolchain, Toolchain};
pub use workspace::{ManagedWorkspace, WorkspaceManifest};
