//! End-to-end native backtest coverage over the market-lake read route.
//!
//! Each test binds a local fake of the frozen routes, drives the native engine
//! through [`run_native_job_with_lake_client`] with a deterministic in-test
//! strategy, and asserts the run consumed the real lake events rather than a
//! synthetic benchmark. It also asserts the native path emits the complete
//! result set — `metrics.json`, the offline `report.html`, and the recorder's
//! Arrow-readable `equity.parquet` time series — and that two runs over the
//! same lake replay byte-identically. No network and no lake files are touched.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::{
    BacktestOutcome, EngineMarket, EngineOptions, EngineOrder, StrategyInstance, StrategyRuntime,
    StrategyStart,
};
use backtest::backtest::errors::{BacktestError, ONE_DAY_NS};
use backtest::backtest::job::{
    lake_fixture, run_native_job_with_lake_client, run_native_study_job_with_lake_client,
};
use harness_lakeclient::testing::{
    bars_stream, quote_stream, trade_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer,
    FakeQuoteRow, FakeTradeRow,
};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};
use marketdata::Date;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

const TOKEN: &str = "backtest-lake-token";

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

/// The run window's two-event lake fixture: one quote and one trade.
fn fixture_rows() -> (i64, i64) {
    let date = Date::from_ymd(2024, 1, 2).unwrap();
    let day = i64::from(date.days());
    let start_ns = day * ONE_DAY_NS + (14 * 3_600 + 30 * 60) * 1_000_000_000;
    let end_ns = (day + 1) * ONE_DAY_NS;
    (start_ns, end_ns)
}

fn fake_lake() -> FakeLakeServer {
    let (start_ns, _) = fixture_rows();
    let quotes = vec![FakeQuoteRow::new(
        start_ns, "450.00", "450.02", 100, 100, "XNYS",
    )];
    let trades = vec![FakeTradeRow::new(
        start_ns + 1_000_000_000,
        1,
        "450.01",
        50,
        "XNYS",
    )];
    FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([
            ("quote".to_string(), quote_stream("SPY", &quotes)),
            ("trade".to_string(), trade_stream("SPY", &trades)),
        ]),
        ..FakeLakeConfig::new(TOKEN)
    })
}

fn lake_client(server: &FakeLakeServer) -> BlockingLakeClient {
    BlockingLakeClient::new(LakeClientConfig::new(server.base_url(), TOKEN).expect("client config"))
        .expect("blocking client")
}

/// Run one native lake-backed job with the deterministic in-test strategy.
fn run_lake_job(server: &FakeLakeServer, output: &Path) -> (BacktestOutcome, PathBuf) {
    let (start_ns, end_ns) = fixture_rows();
    let mut configuration = BacktestConfig::new("lake-run", start_ns, end_ns, 7).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let strategies = vec![StrategyInstance {
        instance_id: "lake-strategy".to_string(),
        runtime: Box::new(BuyOnce { submitted: false }),
    }];
    let client = lake_client(server);
    run_native_job_with_lake_client(
        &configuration,
        &EngineOptions::default(),
        Some(output),
        strategies,
        &client,
    )
    .expect("native lake job")
}

#[test]
fn runs_the_job_against_the_market_lake_read_route() {
    let directory = temp_dir("run");
    let output = directory.join("out");
    let server = fake_lake();
    let (outcome, artifact_path) = run_lake_job(&server, &output);

    // The engine consumed the two real lake events and filled the buy.
    assert!(outcome.completed());
    assert_eq!(outcome.market_event_count, 2);
    assert_eq!(outcome.dispatched_count, 5);
    assert_eq!(outcome.order_count, 1);
    assert_eq!(outcome.fills.len(), 1);
    assert!(artifact_path.exists());
    let document = std::fs::read_to_string(&artifact_path).expect("artifact");
    assert!(document.contains("\"market_events\":2"));

    // The route was read through ensure plus the Arrow IPC read.
    let paths: Vec<String> = server
        .requests()
        .iter()
        .map(|request| request.path.clone())
        .collect();
    assert!(
        paths.contains(&"/v1/catalog/ensure".to_string()),
        "{paths:?}"
    );
    assert!(paths.contains(&"/v1/read".to_string()), "{paths:?}");

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
fn lake_backtest_replays_deterministically() {
    let directory = temp_dir("replay");
    let first = directory.join("first");
    let second = directory.join("second");
    let server = fake_lake();
    let (first_outcome, first_path) = run_lake_job(&server, &first);
    let (second_outcome, second_path) = run_lake_job(&server, &second);

    assert_eq!(first_outcome.dispatch_hash, second_outcome.dispatch_hash);
    assert_eq!(
        first_outcome.artifact_digest(),
        second_outcome.artifact_digest()
    );
    assert_eq!(
        std::fs::read(&first_path).expect("first artifact"),
        std::fs::read(&second_path).expect("second artifact")
    );
    for name in ["metrics.json", "report.html"] {
        assert_eq!(
            std::fs::read(first.join(name)).expect("first artifact"),
            std::fs::read(second.join(name)).expect("second artifact"),
            "{name} differs"
        );
    }
    assert_eq!(
        std::fs::read(first.join("artifacts").join("equity.parquet")).expect("first equity"),
        std::fs::read(second.join("artifacts").join("equity.parquet")).expect("second equity")
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn an_empty_lake_window_is_a_clear_error() {
    let server = FakeLakeServer::start(FakeLakeConfig::new(TOKEN));
    let config_ns = i64::from(Date::from_ymd(2024, 1, 2).unwrap().days()) * ONE_DAY_NS;
    let mut configuration =
        BacktestConfig::new("empty-lake", config_ns, config_ns + ONE_DAY_NS, 1).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let client = lake_client(&server);
    let error = lake_fixture(&configuration, &client).expect_err("empty window must fail");
    assert!(
        matches!(error, backtest::backtest::job::BacktestJobError::Io(ref message)
            if message.contains("served no quote or trade events")),
        "expected the loud no-events error, got {error:?}"
    );
}

#[test]
fn a_configured_lake_that_cannot_serve_the_window_fails_the_job() {
    // The route is load-bearing: an unreadable lake must fail the job instead
    // of silently replaying a synthetic benchmark fixture.
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([(
            "quote".to_string(),
            serde_json::to_vec(&serde_json::json!({
                "error": {"code": "storage_unavailable", "message": "the lake read failed"}
            }))
            .expect("error body"),
        )]),
        read_bodies: vec![(
            500,
            serde_json::to_vec(&serde_json::json!({
                "error": {"code": "storage_unavailable", "message": "the lake read failed"}
            }))
            .expect("error body"),
        )],
        ..FakeLakeConfig::new(TOKEN)
    });
    let config_ns = i64::from(Date::from_ymd(2024, 1, 2).unwrap().days()) * ONE_DAY_NS;
    let mut configuration =
        BacktestConfig::new("empty-lake-job", config_ns, config_ns + ONE_DAY_NS, 1)
            .expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let client = lake_client(&server);
    let error = run_native_job_with_lake_client(
        &configuration,
        &EngineOptions::default(),
        None,
        Vec::new(),
        &client,
    )
    .expect_err("an unreadable lake must fail the job");
    assert!(
        matches!(error, backtest::backtest::job::BacktestJobError::Io(_)),
        "expected the lake read error, got {error:?}"
    );
}

/// The fixture decode path is exercised directly: a `no_data` route read maps
/// to no batches and therefore a loud empty-window failure, never synthetic
/// rows.
#[test]
fn no_data_route_reads_never_fall_back_to_synthetic_events() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies: vec![(404, harness_lakeclient::testing::no_data_body())],
        ..FakeLakeConfig::new(TOKEN)
    });
    let config_ns = i64::from(Date::from_ymd(2024, 1, 2).unwrap().days()) * ONE_DAY_NS;
    let mut configuration =
        BacktestConfig::new("no-data", config_ns, config_ns + ONE_DAY_NS, 1).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let client = lake_client(&server);
    let error = lake_fixture(&configuration, &client).expect_err("no_data must fail");
    assert!(
        matches!(error, backtest::backtest::job::BacktestJobError::Io(_)),
        "expected an Io error, got {error:?}"
    );
}

/// The decode boundary maps the frozen `equity.trade` schema's provider column
/// names (`size`, `exchange`, integer `trade_id`) onto the event model.
#[test]
fn decoded_route_events_carry_provider_fields() {
    let server = fake_lake();
    let client = lake_client(&server);
    let (start_ns, end_ns) = fixture_rows();
    let mut configuration = BacktestConfig::new("decode", start_ns, end_ns, 1).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let fixture = lake_fixture(&configuration, &client).expect("fixture");
    assert_eq!(fixture.events.len(), 2);
    let trade = fixture
        .events
        .iter()
        .find_map(|event| match event {
            backtest::backtest::fixture::MarketEvent::Trade {
                price,
                quantity,
                venue,
                trade_id,
                ..
            } => Some((price, quantity, venue, trade_id)),
            _ => None,
        })
        .expect("trade event");
    assert_eq!(trade.0.to_string(), "450.010000000000000000");
    assert_eq!(
        *trade.1,
        num::Decimal18::from_i64(50),
        "the provider `size` column carries the quantity"
    );
    assert_eq!(trade.2.as_str(), "XNYS");
    assert_eq!(trade.3.as_str(), "1");
    // The fixture source identity names the route endpoint, not a directory.
    assert!(
        fixture.source_id.starts_with("lake:http://127.0.0.1"),
        "{:?}",
        fixture.source_id
    );
}

/// The quote decode honors the wire `data_type` when mapping sizes and venue.
#[test]
fn decoded_quote_events_use_the_wire_data_type() {
    let server = fake_lake();
    let client = lake_client(&server);
    let (start_ns, end_ns) = fixture_rows();
    let mut configuration =
        BacktestConfig::new("decode-quote", start_ns, end_ns, 1).expect("config");
    configuration.symbols = vec!["SPY".to_string()];
    let fixture = lake_fixture(&configuration, &client).expect("fixture");
    let quote = fixture
        .events
        .iter()
        .find_map(|event| match event {
            backtest::backtest::fixture::MarketEvent::Quote {
                bid, ask, venue, ..
            } => Some((bid.to_string(), ask.to_string(), venue)),
            _ => None,
        })
        .expect("quote event");
    assert_eq!(quote.0, "450.000000000000000000");
    assert_eq!(quote.1, "450.020000000000000000");
    assert_eq!(quote.2.as_str(), "XNYS");
    // The fixture routes quote and trade reads by the wire data type.
    let data_types: Vec<String> = server
        .requests()
        .iter()
        .filter(|request| request.path == "/v1/read")
        .filter_map(|request| request.query.get("data_type").cloned())
        .collect();
    assert!(data_types.contains(&"quote".to_string()), "{data_types:?}");
    assert!(data_types.contains(&"trade".to_string()), "{data_types:?}");
}

/// A study runtime that folds bars and fails loud on any tick.
struct BarCountingStudy {
    bars: u64,
}

impl StrategyRuntime for BarCountingStudy {
    fn on_start(&mut self, _start: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }

    fn on_market(&mut self, _event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        Err(BacktestError::StrategyExecution(
            "studies consume bars only".to_string(),
        ))
    }

    fn on_bar(
        &mut self,
        _bar: &backtest::backtest::engine_core::EngineBar,
    ) -> Result<Vec<EngineOrder>, BacktestError> {
        self.bars += 1;
        Ok(Vec::new())
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        BTreeMap::from([("bar_count".to_string(), self.bars.to_string())])
    }
}

/// Three SPY bars: one before the window and two inside it.
fn study_bar_rows(start_ns: i64) -> Vec<FakeBarRow> {
    vec![
        FakeBarRow::new(
            start_ns - 60_000_000_000,
            "99.00",
            "99.50",
            "98.50",
            "99.00",
            "100.00",
        ),
        FakeBarRow::new(start_ns, "100.00", "100.50", "99.50", "100.25", "1000.00"),
        FakeBarRow::new(
            start_ns + 120_000_000_000,
            "100.25",
            "101.00",
            "100.00",
            "100.75",
            "1200.00",
        ),
    ]
}

fn study_configuration(run_id: &str, interval: &str) -> (BacktestConfig, i64, i64) {
    let (start_ns, end_ns) = fixture_rows();
    let mut configuration = BacktestConfig::new(run_id, start_ns, end_ns, 7).expect("config");
    configuration.shell = "study".to_string();
    configuration.data_bar_interval = interval.to_string();
    configuration.symbols = vec!["SPY".to_string()];
    (configuration, start_ns, end_ns)
}

#[test]
fn study_fixture_selects_bars_by_interval_and_window() {
    let (start_ns, _) = fixture_rows();
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([(
            "bars".to_string(),
            bars_stream("SPY", &study_bar_rows(start_ns)),
        )]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = lake_client(&server);
    let (configuration, _, _) = study_configuration("study-bars", "1min");

    let fixture = lake_fixture(&configuration, &client).expect("study fixture");
    assert_eq!(
        fixture.events.len(),
        2,
        "the pre-window bar must be filtered"
    );
    assert!(
        fixture
            .events
            .iter()
            .all(|event| matches!(event, backtest::backtest::fixture::MarketEvent::Bar { .. })),
        "{:?}",
        fixture.events
    );
    let timestamps: Vec<i64> = fixture
        .events
        .iter()
        .map(|event| event.timestamp_ns())
        .collect();
    let mut sorted = timestamps.clone();
    sorted.sort_unstable();
    assert_eq!(timestamps, sorted, "bars must replay in timestamp order");

    // The route requests bars with the configured interval token.
    let ensure_body: serde_json::Value = serde_json::from_slice(
        server
            .requests()
            .iter()
            .find(|request| request.path == "/v1/catalog/ensure")
            .and_then(|request| request.body.as_deref())
            .expect("ensure body"),
    )
    .expect("ensure json");
    assert_eq!(ensure_body["data_type"], "bars");
    assert_eq!(ensure_body["symbol"], "SPY");
    assert_eq!(ensure_body["interval"], "1min");
    let read_query = server
        .requests()
        .iter()
        .find(|request| request.path == "/v1/read")
        .expect("read request")
        .query
        .clone();
    assert_eq!(
        read_query.get("data_type").map(String::as_str),
        Some("bars")
    );
    assert_eq!(read_query.get("interval").map(String::as_str), Some("1min"));
}

#[test]
fn study_fixture_rejects_intervals_outside_the_shared_vocabulary() {
    let server = FakeLakeServer::start(FakeLakeConfig::new(TOKEN));
    let client = lake_client(&server);
    let (configuration, _, _) = study_configuration("bad-interval", "1h");
    let error = lake_fixture(&configuration, &client).expect_err("1h is not a canonical token");
    assert!(
        error.to_string().contains("1min, 1d"),
        "the error must name the canonical tokens: {error}"
    );

    // Config loading rejects the same token before any lake traffic.
    let directory = temp_dir("bad-interval-config");
    let path = directory.join("study.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "shell": "study",
            "run_id": "bad-interval-config",
            "data": {"symbols": ["SPY"], "bar_interval": "1h"}
        }))
        .expect("config json"),
    )
    .expect("write config");
    let error =
        backtest::backtest::job::load_study_config(&path).expect_err("the loader must reject 1h");
    assert!(
        error.to_string().contains("1min, 1d"),
        "the loader error must name the canonical tokens: {error}"
    );
    // The same token stays valid for backtests, which replay ticks.
    let path = directory.join("backtest.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "shell": "backtest",
            "run_id": "backtest-interval",
            "data": {"symbols": ["SPY"], "bar_interval": "1h"}
        }))
        .expect("config json"),
    )
    .expect("write config");
    backtest::backtest::job::load_backtest_config(&path).expect("backtests keep their behavior");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn runs_a_study_job_over_the_bars_route() {
    let directory = temp_dir("study-run");
    let output = directory.join("out");
    let (start_ns, _) = fixture_rows();
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([(
            "bars".to_string(),
            bars_stream("SPY", &study_bar_rows(start_ns)),
        )]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = lake_client(&server);
    let (configuration, _, _) = study_configuration("study-job-run", "1min");
    let programs = vec![StrategyInstance {
        instance_id: "bar-study".to_string(),
        runtime: Box::new(BarCountingStudy { bars: 0 }),
    }];

    let (outcome, artifact_path) = run_native_study_job_with_lake_client(
        &configuration,
        &EngineOptions::default(),
        Some(output.as_path()),
        programs,
        &client,
    )
    .expect("native study job");

    // The engine dispatched both in-window bars through the bar entry.
    assert!(outcome.completed());
    assert_eq!(outcome.market_event_count, 2);
    assert_eq!(
        outcome.custom_metrics.get("bar_count").map(String::as_str),
        Some("2")
    );
    assert!(artifact_path.ends_with("study.json"));
    let document = std::fs::read_to_string(&artifact_path).expect("study artifact");
    assert!(document.contains("\"status\":\"completed\""), "{document}");
    assert!(document.contains("\"custom_metrics\""), "{document}");
    assert!(document.contains("\"bar_count\":\"2\""), "{document}");
    let metrics = std::fs::read_to_string(output.join("metrics.json")).expect("metrics.json");
    assert!(metrics.contains("\"bar_count\":\"2\""), "{metrics}");
    let _ = std::fs::remove_dir_all(&directory);
}
