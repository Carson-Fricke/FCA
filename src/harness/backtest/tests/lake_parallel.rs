//! Multi-symbol lake acquisition for a study window.
//!
//! Every configured symbol's ensure and read are issued through the batch
//! helpers; the merged replay stays canonical because `lake_fixture` sorts the
//! events after acquiring them.

use std::collections::BTreeMap;

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::engine_core::MarketSubscription;
use backtest::backtest::job::{lake_fixture_with_subscriptions, parse_timestamp_ns};
use harness_lakeclient::testing::{
    bars_stream, quote_stream, trade_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer,
    FakeQuoteRow, FakeTradeRow,
};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};

const TOKEN: &str = "parallel-study-token";

fn stamp(text: &str) -> i64 {
    parse_timestamp_ns(text).expect("timestamp")
}

#[test]
fn studies_acquire_exact_inputs_and_backtests_add_tick_execution_feeds() {
    for (shell, kinds) in ["study", "backtest"].into_iter().flat_map(|shell| {
        [
            vec!["bars"],
            vec!["quote", "bars"],
            vec!["trade"],
            vec!["quote"],
        ]
        .into_iter()
        .map(move |kinds| (shell, kinds))
    }) {
        let start = config().start_ns;
        let server = FakeLakeServer::start(FakeLakeConfig {
            read_bodies_by_type: BTreeMap::from([
                ("bars".into(), bars_stream("SPY", &[bar_row(0)])),
                (
                    "quote".into(),
                    quote_stream(
                        "SPY",
                        &[FakeQuoteRow::new(start, "10", "11", 100, 100, "XNYS")],
                    ),
                ),
                (
                    "trade".into(),
                    trade_stream("SPY", &[FakeTradeRow::new(start, 1, "10", 100, "XNYS")]),
                ),
            ]),
            ..FakeLakeConfig::new(TOKEN)
        });
        let inputs: Vec<_> = kinds
            .iter()
            .map(|kind| MarketSubscription {
                data_type: (*kind).into(),
                symbols: vec!["SPY".into()],
                interval: (*kind == "bars").then(|| "1min".into()),
            })
            .collect();
        let mut configuration = config();
        configuration.shell = shell.into();
        configuration.data_subscriptions = inputs.clone();
        configuration.data_subscriptions_declared = true;
        let fixture =
            lake_fixture_with_subscriptions(&configuration, &client(&server), &inputs).unwrap();
        let mut acquired = kinds.clone();
        if shell == "backtest" {
            acquired.extend(["quote", "trade"]);
        }
        acquired.sort();
        acquired.dedup();
        assert_eq!(fixture.events.len(), acquired.len());
        let requests = server.requests();
        let ensures: Vec<_> = requests
            .iter()
            .filter(|r| r.path == "/v1/catalog/ensure")
            .collect();
        assert_eq!(ensures.len(), acquired.len());
        for request in ensures {
            let body: serde_json::Value =
                serde_json::from_slice(request.body.as_ref().unwrap()).unwrap();
            assert!(acquired.contains(&body["data_type"].as_str().unwrap()));
            assert_eq!(body["symbol"], "SPY");
        }
        assert_eq!(
            requests.iter().filter(|r| r.path == "/v1/read").count(),
            acquired.len()
        );
        for request in requests.iter().filter(|r| r.path == "/v1/read") {
            assert!(acquired.contains(&request.query["data_type"].as_str()));
        }
    }
}

#[test]
fn missing_requested_stream_is_not_covered_by_bars_and_unsupported_intervals_fail_before_io() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([
            ("bars".into(), bars_stream("SPY", &[bar_row(0)])),
            ("trade".into(), trade_stream("SPY", &[])),
        ]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = client(&server);
    let inputs = vec![
        MarketSubscription {
            data_type: "bars".into(),
            symbols: vec!["SPY".into()],
            interval: Some("1min".into()),
        },
        MarketSubscription {
            data_type: "trade".into(),
            symbols: vec!["SPY".into()],
            interval: None,
        },
    ];
    let error = lake_fixture_with_subscriptions(&config(), &client, &inputs).unwrap_err();
    assert!(error.to_string().contains("trade/SPY coverage"));
    let before = server.requests().len();
    let mut conflicting = inputs;
    conflicting.push(MarketSubscription {
        data_type: "bars".into(),
        symbols: vec!["SPY".into()],
        interval: Some("1s".into()),
    });
    assert!(lake_fixture_with_subscriptions(&config(), &client, &conflicting).is_err());
    assert_eq!(server.requests().len(), before);
}

#[test]
fn acquisition_rejects_bars_without_completed_frame_coverage() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([("bars".into(), bars_stream("SPY", &[bar_row(0)]))]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let mut config = config();
    config.end_ns = config.start_ns + 30_000_000_000;
    let inputs = vec![MarketSubscription {
        data_type: "bars".into(),
        symbols: vec!["SPY".into()],
        interval: Some("1min".into()),
    }];
    let error = lake_fixture_with_subscriptions(&config, &client(&server), &inputs).unwrap_err();
    assert!(error
        .to_string()
        .contains("bars/SPY completed frame coverage"));
}

#[test]
fn exclusive_end_dates_and_nanosecond_row_bounds_are_exact() {
    let start = stamp("2024-01-02T14:30:00.000000001Z");
    for (end, expected_date) in [
        ("2024-01-03T00:00:00Z", "2024-01-02"),
        ("2024-01-03T00:00:00.000000001Z", "2024-01-03"),
        ("2024-01-03T14:30:00.000000001Z", "2024-01-03"),
    ] {
        let end_ns = stamp(end);
        let server = FakeLakeServer::start(FakeLakeConfig {
            read_bodies_by_type: BTreeMap::from([(
                "quote".into(),
                quote_stream(
                    "SPY",
                    &[
                        FakeQuoteRow::new(start - 1, "10", "11", 100, 100, "XNYS"),
                        FakeQuoteRow::new(start, "10", "11", 100, 100, "XNYS"),
                        FakeQuoteRow::new(end_ns - 1, "10", "11", 100, 100, "XNYS"),
                        FakeQuoteRow::new(end_ns, "10", "11", 100, 100, "XNYS"),
                    ],
                ),
            )]),
            ..FakeLakeConfig::new(TOKEN)
        });
        let mut config = config();
        config.start_ns = start;
        config.end_ns = end_ns;
        let inputs = vec![MarketSubscription {
            data_type: "quote".into(),
            symbols: vec!["SPY".into()],
            interval: None,
        }];
        let fixture = lake_fixture_with_subscriptions(&config, &client(&server), &inputs).unwrap();
        assert_eq!(
            fixture
                .events
                .iter()
                .map(|event| event.timestamp_ns())
                .collect::<Vec<_>>(),
            [start, end_ns - 1]
        );
        for request in server.requests() {
            if request.path == "/v1/catalog/ensure" {
                let body: serde_json::Value =
                    serde_json::from_slice(request.body.as_ref().unwrap()).unwrap();
                assert_eq!(body["start_date"], "2024-01-02");
                assert_eq!(body["end_date"], expected_date);
            } else if request.path == "/v1/read" {
                assert_eq!(request.query["start_date"], "2024-01-02");
                assert_eq!(request.query["end_date"], expected_date);
            }
        }
    }
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

    let subscriptions = vec![MarketSubscription {
        data_type: "bars".to_string(),
        symbols: vec!["SPY".to_string(), "QQQ".to_string()],
        interval: Some("1min".to_string()),
    }];
    let fixture = lake_fixture_with_subscriptions(&config(), &client(&server), &subscriptions)
        .expect("lake fixture");

    // The fake serves one body per data type; every event is re-keyed to its
    // configured symbol, and the merged vector is sorted by
    // (timestamp, sequence, class priority) regardless of read completion order.
    let symbols: Vec<&str> = fixture
        .events
        .iter()
        .map(backtest::backtest::fixture::MarketEvent::symbol)
        .collect();
    assert_eq!(symbols, vec!["QQQ", "SPY", "QQQ", "SPY"]);
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

#[test]
fn multiple_timeframes_acquire_one_canonical_minute_source_window() {
    let rows: Vec<_> = (0..390).map(bar_row).collect();
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([("bars".into(), bars_stream("SPY", &rows))]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let subscriptions: Vec<_> = ["1min", "5min", "1d"]
        .into_iter()
        .map(|interval| MarketSubscription {
            data_type: "bars".into(),
            symbols: vec!["SPY".into()],
            interval: Some(interval.into()),
        })
        .collect();
    let fixture =
        lake_fixture_with_subscriptions(&config(), &client(&server), &subscriptions).unwrap();
    assert_eq!(fixture.events.len(), 390);
    let requests = server.requests();
    let ensures: Vec<_> = requests
        .iter()
        .filter(|request| request.path == "/v1/catalog/ensure")
        .collect();
    assert_eq!(ensures.len(), 1);
    let body: serde_json::Value =
        serde_json::from_slice(ensures[0].body.as_ref().unwrap()).unwrap();
    assert_eq!(body["interval"], "1min");
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.path == "/v1/read")
            .count(),
        1
    );
}
