//! Multi-symbol lake acquisition for a study window.
//!
//! Every configured symbol's ensure and read are issued through the batch
//! helpers; the merged replay stays canonical because `lake_fixture` sorts the
//! events after acquiring them.

use std::collections::BTreeMap;

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::job::{lake_fixture, parse_timestamp_ns};
use harness_lakeclient::testing::{bars_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};

const TOKEN: &str = "parallel-study-token";

fn stamp(text: &str) -> i64 {
    parse_timestamp_ns(text).expect("timestamp")
}

fn config() -> BacktestConfig {
    let mut config = BacktestConfig::new(
        "parallel",
        stamp("2024-01-02T14:30:00Z"),
        stamp("2024-01-02T21:00:00Z"),
        7,
    )
    .expect("configuration");
    config.shell = "study".to_string();
    config.data_bar_interval = "1min".to_string();
    config.symbols = vec!["SPY".to_string(), "QQQ".to_string()];
    config
}

fn client(server: &FakeLakeServer) -> BlockingLakeClient {
    BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), TOKEN).expect("lake client config"),
    )
    .expect("blocking lake client")
}

fn bar_row(minute: i64) -> FakeBarRow {
    FakeBarRow::new(
        stamp("2024-01-02T14:30:00Z") + minute * 60_000_000_000,
        "10",
        "12",
        "9",
        "11",
        "100",
    )
}

#[test]
fn multi_symbol_study_acquires_every_symbol_and_keeps_canonical_order() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([(
            "bars".to_string(),
            bars_stream("SPY", &[bar_row(1), bar_row(0)]),
        )]),
        ..FakeLakeConfig::new(TOKEN)
    });

    let fixture = lake_fixture(&config(), &client(&server)).expect("lake fixture");

    // The fake serves one body per data type; every event is re-keyed to its
    // configured symbol, and the merged vector is sorted by
    // (timestamp, sequence, class priority) regardless of read completion order.
    let symbols: Vec<&str> = fixture
        .events
        .iter()
        .map(backtest::backtest::fixture::MarketEvent::symbol)
        .collect();
    assert_eq!(symbols, vec!["SPY", "QQQ", "SPY", "QQQ"]);
    let timestamps: Vec<i64> = fixture
        .events
        .iter()
        .map(backtest::backtest::fixture::MarketEvent::timestamp_ns)
        .collect();
    let mut sorted = timestamps.clone();
    sorted.sort_unstable();
    assert_eq!(timestamps, sorted);

    let requests = server.requests();
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.path == "/v1/catalog/ensure")
            .count(),
        2
    );
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.path == "/v1/read")
            .count(),
        2
    );
}
