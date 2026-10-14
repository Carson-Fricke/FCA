//! Resolver acknowledgements must match the requested input before acquisition.
use backtest::backtest::{
    config::BacktestConfig,
    engine_core::MarketSubscription,
    job::{lake_fixture_with_subscriptions, parse_timestamp_ns},
};
use harness_lakeclient::{
    testing::{FakeLakeConfig, FakeLakeServer},
    BlockingLakeClient, LakeClientConfig,
};
use serde_json::json;
use std::collections::BTreeMap;

#[test]
fn mismatched_identity_future_quotes_and_wrong_delta_stop_before_market_reads() {
    let start = parse_timestamp_ns("2024-01-19T14:30:00Z").unwrap();
    let mut config =
        BacktestConfig::new("selection-ack", start, start + 60_000_000_000, 1).unwrap();
    config.symbols = vec!["ATM".into()];
    config.option_selectors=BTreeMap::from([("ATM".into(),serde_json::from_value(json!({
        "underlying":"SPY","right":"call","as_of":"2024-01-18T21:00:00Z","days_to_expiration":30,"delta":"0.50"
    })).unwrap())]);
    let valid = json!({"symbol":"SPY240216C00100000","underlying":"SPY","expiration":"2024-02-16","strike":"100","right":"call","multiplier":100,"as_of":"2024-01-18T21:00:00Z","delta":"0.51","quote_time":"2024-01-18T20:59:00Z","method":"test"});
    for (field, value) in [
        ("strike", "101"),
        ("underlying", "QQQ"),
        ("quote_time", "2024-01-19T15:00:00Z"),
        ("delta", "0.80"),
        ("delta", "-0.50"),
    ] {
        let mut resolution = valid.clone();
        resolution[field] = json!(value);
        let server = FakeLakeServer::start(FakeLakeConfig {
            option_resolution: Some(resolution),
            ..FakeLakeConfig::new("test")
        });
        let client =
            BlockingLakeClient::new(LakeClientConfig::new(server.base_url(), "test").unwrap())
                .unwrap();
        let inputs = vec![MarketSubscription {
            data_type: "quote".into(),
            symbols: vec!["ATM".into()],
            interval: None,
        }];
        assert!(lake_fixture_with_subscriptions(&config, &client, &inputs).is_err());
        assert_eq!(server.requests().len(), 1);
        assert_eq!(server.requests()[0].path, "/v1/options/resolve");
    }
}
