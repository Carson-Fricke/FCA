//! A strategy instance's starting cash comes from its configured capital
//! fraction, or the run fails by name.
//!
//! A declared fraction that is not an exact decimal fails the job before any
//! artifact is written; it never runs at full cash. A well-formed fraction runs
//! to completion through the same job boundary.

use std::collections::BTreeMap;
use std::path::PathBuf;

use backtest::backtest::config::parse_timestamp_ns;
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::config::StrategyInstanceConfig;
use backtest::backtest::engine_core::EngineOptions;
use harness_lakeclient::testing::quote_parquet;
use harness_lakeclient::testing::FakeLakeConfig;
use harness_lakeclient::testing::FakeLakeServer;
use harness_lakeclient::testing::FakeQuoteRow;
use harness_lakeclient::BlockingLakeClient;
use harness_lakeclient::LakeClientConfig;
use harness_research::error::BacktestJobError;
use harness_research::replay::run_native_job_with_lake_client;
use harness_trading::program::EngineMarket;
use harness_trading::program::EngineOrder;
use harness_trading::program::MarketSubscription;
use harness_trading::program::StrategyInstance;
use harness_trading::program::StrategyRuntime;
use harness_trading::program::StrategyStart;
use harness_trading::replay_errors::BacktestError;

const TOKEN: &str = "capital-fraction-token";

/// A strategy that subscribes to SPY quotes and never submits an order.
struct Idle;

impl StrategyRuntime for Idle {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(vec![MarketSubscription {
            data_type: "quote".into(),
            symbols: vec!["SPY".into()],
            interval: None,
        }])
    }

    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }

    fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        Ok(Vec::new())
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}

/// A one-instance configuration whose instance declares `capital_fraction`.
fn configuration(capital_fraction: &str) -> BacktestConfig {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let end = parse_timestamp_ns("2024-01-02T15:00:00Z").expect("end");
    let mut configuration =
        BacktestConfig::new("capital-fraction", start, end, 7).expect("configuration");
    configuration.symbols = vec!["SPY".to_string()];
    configuration.strategy_instances = vec![StrategyInstanceConfig {
        instance_id: "trader".to_string(),
        implementation: "idle".to_string(),
        bundle_hash: "idle-bundle".to_string(),
        parameters: BTreeMap::new(),
        symbols: vec!["SPY".to_string()],
        capital_fraction: capital_fraction.to_string(),
    }];
    configuration
}

/// Run the idle strategy over one quote. Returns whether the run completed,
/// and the output directory the job would write into.
fn run(name: &str, configuration: &BacktestConfig) -> (Result<bool, BacktestJobError>, PathBuf) {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let server = FakeLakeServer::start(FakeLakeConfig {
        partitions_by_type: BTreeMap::from([(
            "quote".to_string(),
            quote_parquet(
                "SPY",
                &[FakeQuoteRow::new(start, "9", "10", 100, 100, "XNYS")],
            ),
        )]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), TOKEN).expect("lake client config"),
    )
    .expect("blocking lake client");
    let directory = std::env::temp_dir().join(format!("backtest-capital-fraction-{name}"));
    let _ = std::fs::remove_dir_all(&directory);
    let result = run_native_job_with_lake_client(
        configuration,
        &EngineOptions::default(),
        Some(&directory),
        vec![StrategyInstance {
            instance_id: "trader".into(),
            runtime: Box::new(Idle),
        }],
        &client,
    )
    .map(|(outcome, _)| outcome.completed());
    (result, directory)
}

#[test]
fn malformed_capital_fraction_fails_the_job_and_writes_no_artifact() {
    let (result, directory) = run("malformed", &configuration("half"));
    let error = result.expect_err("a malformed capital fraction must fail the run");
    assert!(
        matches!(error, BacktestJobError::Engine(ref message) if message.contains("capital_fraction"))
    );
    assert!(!directory.join("backtest.json").exists());
}

#[test]
fn declared_capital_fraction_runs_the_job_to_completion() {
    let (result, _) = run("declared", &configuration("0.5"));
    assert!(result.expect("a well-formed capital fraction runs"));
}

#[test]
fn negative_or_overallocated_capital_fails_before_publication() {
    for fraction in ["-0.5", "1.1"] {
        let (result, directory) = run(
            &format!("invalid-allocation-{fraction}"),
            &configuration(fraction),
        );
        assert!(matches!(result, Err(BacktestJobError::Engine(_))));
        assert!(!directory.join("backtest.json").exists());
        assert!(!directory.join("metrics.json").exists());
    }
}
