//! End-to-end `backtest-job --data-dir` coverage over a local Parquet lake.
//!
//! This test materializes a tiny Hive lake on disk, drives the native engine
//! through [`run_native_job_with_data_dir`] with a deterministic in-test
//! strategy, and asserts the run consumed the real lake events rather than the
//! synthetic benchmark. It opens no sockets and reads no wall clock.

use std::path::PathBuf;

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{
    EngineMarket, EngineOptions, EngineOrder, StrategyInstance, StrategyRuntime, StrategyStart,
};
use backtest::backtest::errors::{BacktestError, ONE_DAY_NS};
use backtest::backtest::job::{lake_fixture, run_native_job_with_data_dir};
use marketdata::events::{MarketEvent as LakeEvent, QuoteEvent, TradeEvent};
use marketdata::time::DateTime;
use marketdata::{write_lake_partition, AssetClass, DataType, Date};

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

#[test]
fn runs_the_job_against_a_local_parquet_lake() {
    let directory = temp_dir("run");
    let lake = directory.join("lake");
    let output = directory.join("out");
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
        &lake,
        date,
        AssetClass::Equity,
        DataType::Quote,
        "SPY",
        &[quote],
    )
    .expect("write quote lake");
    write_lake_partition(
        &lake,
        date,
        AssetClass::Equity,
        DataType::Trade,
        "SPY",
        &[trade],
    )
    .expect("write trade lake");

    let mut configuration = BacktestConfig::new("lake-run", start_ns, end_ns, 7).expect("config");
    configuration.symbols = vec!["SPY".to_string()];

    // The fixture resolves both lake partitions and both events.
    let fixture = lake_fixture(&configuration, &lake).expect("lake fixture");
    assert_eq!(fixture.events.len(), 2);
    assert!(fixture.source_id.starts_with("lake:"));
    assert_eq!(fixture.sessions.len(), 1);

    let strategies = vec![StrategyInstance {
        instance_id: "lake-strategy".to_string(),
        runtime: Box::new(BuyOnce { submitted: false }),
    }];
    let (outcome, artifact_path) = run_native_job_with_data_dir(
        &configuration,
        &EngineOptions::default(),
        Some(&output),
        strategies,
        Some(&lake),
    )
    .expect("native lake job");

    // The engine consumed the two real lake events and filled the buy.
    assert!(outcome.completed());
    assert_eq!(outcome.market_event_count, 2);
    assert_eq!(outcome.dispatched_count, 5);
    assert_eq!(outcome.order_count, 1);
    assert_eq!(outcome.fills.len(), 1);
    assert_eq!(outcome.preflight.source_id, fixture.source_id);
    assert!(artifact_path.exists());
    let document = std::fs::read_to_string(&artifact_path).expect("artifact");
    assert!(document.contains("\"market_events\":2"));

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
