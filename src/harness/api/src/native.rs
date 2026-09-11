//! Native backtest engine runner for the `backtest-job` command surface.
//!
//! [`NativeBacktestJobRunner`] plugs the ported Rust engine into the CLI seam
//! defined by [`crate::BacktestJobRunner`]. The engine core (clock, event bus,
//! replay, matching, fees, portfolio, risk, and results) is native Rust; the
//! configured strategies stay Python and are driven through the
//! `harness-host` PyO3 plugin adapter.
//!
//! The native path is verified by the golden parity suite in
//! `harness-backtest/tests/parity.rs`, which runs this same entry point over
//! the committed benchmark configuration. `--data-dir` is served by the local
//! Parquet/Arrow lake reader in the `marketdata` crate, so a run reads the
//! materialized Hive partitions instead of the synthetic benchmark.

use std::path::PathBuf;

use backtest::backtest::engine_core::BacktestOutcome;

use crate::{BacktestJobArgs, BacktestJobError, BacktestJobRunner, BacktestRunSummary};

/// Run `backtest-job` through the native Rust engine and Python strategy host.
#[derive(Debug, Clone, Copy, Default)]
pub struct NativeBacktestJobRunner;

impl BacktestJobRunner for NativeBacktestJobRunner {
    fn run(&self, args: &BacktestJobArgs) -> Result<BacktestRunSummary, BacktestJobError> {
        let Some(config) = args.config.as_deref() else {
            return Err(BacktestJobError::Engine(
                "A backtest configuration is required: backtest-job --config <path>".to_string(),
            ));
        };
        let (outcome, artifact_path) = host::backtest::run_backtest_job_with_data_dir(
            config,
            args.output_dir.as_deref(),
            args.data_dir.as_deref(),
        )
        .map_err(|error| BacktestJobError::Engine(error.to_string()))?;
        Ok(summary_from_outcome(&outcome, artifact_path))
    }
}

/// Project one native outcome onto the CLI's canonical stdout summary.
#[must_use]
fn summary_from_outcome(outcome: &BacktestOutcome, artifact_path: PathBuf) -> BacktestRunSummary {
    BacktestRunSummary {
        run_id: outcome.manifest.run_id.clone(),
        status: outcome.status.value().to_string(),
        manifest_hash: outcome.manifest.manifest_hash(),
        artifact_digest: outcome.artifact_digest(),
        outcome_digest: outcome.outcome_digest(),
        market_events: outcome.market_event_count,
        orders: outcome.order_count,
        fills: outcome.fills.len() as u64,
        artifact_path,
        reason: outcome.reason.clone(),
    }
}

#[cfg(test)]
#[path = "../tests/unit/native.rs"]
mod tests;
