//! End-to-end `backtest-job --data-dir` coverage over a local Parquet lake.
//!
//! This test materializes a tiny Hive lake on disk, drives the native engine
//! through [`run_native_job_with_data_dir`] with a deterministic in-test
//! strategy, and asserts the run consumed the real lake events rather than the
//! synthetic benchmark. It also asserts the native path emits the complete
//! result set — `metrics.json`, the offline `report.html`, and the recorder's
//! Arrow-readable `equity.parquet` time series. It opens no sockets and reads no
//! wall clock.

use std::path::{Path, PathBuf};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{
    BacktestOutcome, EngineMarket, EngineOptions, EngineOrder, StrategyInstance, StrategyRuntime,
    StrategyStart,
};
use backtest::backtest::errors::{BacktestError, ONE_DAY_NS};
use backtest::backtest::job::{lake_fixture, run_native_job_with_data_dir};
use marketdata::events::{MarketEvent as LakeEvent, QuoteEvent, TradeEvent};
use marketdata::time::DateTime;
use marketdata::{write_lake_partition, AssetClass, DataType, Date};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

struct BuyOnce {
    submitted: bool,
}

impl StrategyRuntime for BuyOnce {
    fn on_start(&mut self, _start: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }

    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        if self.submitted {
            return Ok(Vec::new());
        }
        let EngineMarket::Quote {
            timestamp_ns,
            sequence,
            symbol,
            ..
        } = event
        else {
            return Ok(Vec::new());
        };
        self.submitted = true;
        Ok(vec![EngineOrder {
            order_id: "lake-buy".to_string(),
            symbol: symbol.clone(),
            side: "buy".to_string(),
            quantity: "10".parse().unwrap(),
            order_type: "market".to_string(),
            time_in_force: "day".to_string(),
            limit_price: None,
            stop_price: None,
            timestamp_ns: *timestamp_ns,
            sequence: *sequence,
            event_id: format!("lake-order-{sequence}"),
        }])
    }
}

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-backtest-lake-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

fn dec(value: &str) -> num::Decimal18 {
    value.parse().unwrap()
}

/// Materialize the two-event lake fixture and return the run window.
fn materialize_lake(lake: &Path) -> (i64, i64) {
    let date = Date::from_ymd(2024, 1, 2).unwrap();
    let day = i64::from(date.days());
    let start_ns = day * ONE_DAY_NS + (14 * 3_600 + 30 * 60) * 1_000_000_000;
    let end_ns = (day + 1) * ONE_DAY_NS;

    let quote = LakeEvent::Quote(QuoteEvent {
        event_id: "q-1".to_string(),
        timestamp: DateTime::from_micros(start_ns / 1_000),
        sequence: 0,
        symbol: "SPY".to_string(),
        bid_price: dec("450.00"),
        ask_price: dec("450.02"),
        bid_size: dec("100"),
        ask_size: dec("100"),
        venue: Some("XNYS".to_string()),
    });
    let trade = LakeEvent::Trade(TradeEvent {
        event_id: "t-1".to_string(),
        timestamp: DateTime::from_micros(start_ns / 1_000 + 1_000_000),
        sequence: 1,
        symbol: "SPY".to_string(),
        price: dec("450.01"),
        quantity: dec("50"),
        venue: Some("XNYS".to_string()),
        trade_id: Some("tid-1".to_string()),
    });
    write_lake_partition(
        lake,
        date,
        AssetClass::Equity,
        DataType::Quote,
        "SPY",
        &[quote],
    )
    .expect("write quote lake");
    write_lake_partition(
        lake,
        date,
        AssetClass::Equity,
        DataType::Trade,
        "SPY",
        &[trade],
    )
    .expect("write trade lake");
    (start_ns, end_ns)
}

/// Run one native lake-backed job with the deterministic in-test strategy.
fn run_lake_job(lake: &Path, output: &Path) -> (BacktestOutcome, PathBuf) {
    let (start_ns, end_ns) = materialize_lake(lake);
    let mut configuration = BacktestConfig::new("lake-run", start_ns, end_ns, 7).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let strategies = vec![StrategyInstance {
        instance_id: "lake-strategy".to_string(),
        runtime: Box::new(BuyOnce { submitted: false }),
    }];
    run_native_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        Some(output),
        strategies,
        Some(lake),
    )
    .expect("native lake job")
}

#[test]
fn runs_the_job_against_a_local_parquet_lake() {
    let directory = temp_dir("run");
    let lake = directory.join("lake");
    let output = directory.join("out");
    let (outcome, artifact_path) = run_lake_job(&lake, &output);

    // The engine consumed the two real lake events and filled the buy.
    assert!(outcome.completed());
    assert_eq!(outcome.market_event_count, 2);
    assert_eq!(outcome.dispatched_count, 5);
    assert_eq!(outcome.order_count, 1);
    assert_eq!(outcome.fills.len(), 1);
    assert!(artifact_path.exists());
    let document = std::fs::read_to_string(&artifact_path).expect("artifact");
    assert!(document.contains("\"market_events\":2"));

    // The native path emits the complete result set beside `backtest.json`.
    let metrics_path = output.join("metrics.json");
    let report_path = output.join("report.html");
    let equity_path = output.join("artifacts").join("equity.parquet");
    assert!(metrics_path.exists(), "metrics.json missing");
    assert!(report_path.exists(), "report.html missing");
    assert!(equity_path.exists(), "equity.parquet missing");

    let metrics: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&metrics_path).expect("read metrics"))
            .expect("metrics.json parses");
    assert_eq!(metrics["run_id"], "lake-run");
    assert_eq!(metrics["counts"]["market_events"], 2);
    assert_eq!(metrics["counts"]["fills"], 1);
    assert!(metrics["equity"]["samples"].as_u64().unwrap_or(0) >= 2);

    let report = std::fs::read_to_string(&report_path).expect("read report");
    assert!(report.starts_with("<!doctype html>"));
    assert!(report.contains("lake-run"));

    let builder =
        ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&equity_path).expect("open"))
            .expect("equity.parquet is Arrow-readable");
    let names: Vec<&str> = builder
        .schema()
        .fields()
        .iter()
        .map(|field| field.name().as_str())
        .collect();
    assert!(names.contains(&"equity"), "{names:?}");
    assert!(builder.metadata().file_metadata().num_rows() >= 2);

    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn missing_lake_events_are_a_clear_error() {
    let directory = temp_dir("empty");
    let config_ns = i64::from(Date::from_ymd(2024, 1, 2).unwrap().days()) * ONE_DAY_NS;
    let mut configuration =
        BacktestConfig::new("empty-lake", config_ns, config_ns + ONE_DAY_NS, 1).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let result = lake_fixture(&configuration, &directory);
    assert!(matches!(
        result,
        Err(backtest::backtest::job::BacktestJobError::Io(_))
    ));
    let _ = std::fs::remove_dir_all(&directory);
}
