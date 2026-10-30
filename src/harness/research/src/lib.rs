//! Owns replay/screening job composition, acquisition and artifact publication.
//! Compilation and loaded program adaptation belong to harness-strategy-jit.
mod config;
mod run;
mod screen;
mod screen_run;
mod source;
pub use config::{build_instances, program_kind, JIT_IMPLEMENTATION, SOURCE_PARAMETER};
pub use run::{
    configuration_subscriptions, inspect_configuration, run_backtest_configuration,
    run_study_configuration, validate_configuration, ConfigurationRunError,
};
pub use screen::{rank_rows, run_screening_source};
pub use screen_run::{
    run_screening_configuration, ScreenOutcome, ScreenRunError, ScreenRunOptions, ScreenStatus,
    SCREEN_ARTIFACT, SCREEN_SCHEMA,
};
pub use source::{
    LakeScreeningSource, ScreeningDataSource, ScreeningDataSourceError, ScreeningLakeReads,
};
pub mod acquisition;
pub mod configuration;
pub mod error;
pub mod ledger;
pub mod market_replay;
pub mod prepared_replay;
pub mod publishing;
pub mod replay;

mod history;
