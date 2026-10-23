//! Research acquisition resolves each option once and retains contract attribution.
use backtest::backtest::config::parse_timestamp_ns;
use backtest::backtest::config::BacktestConfig;
use harness_lakeclient::testing::option_bars_parquet;
use harness_lakeclient::testing::option_quote_parquet;
use harness_lakeclient::testing::option_trade_parquet;
use harness_lakeclient::testing::FakeBarRow;
use harness_lakeclient::testing::FakeLakeConfig;
use harness_lakeclient::testing::FakeLakeServer;
use harness_lakeclient::testing::FakeQuoteRow;
use harness_lakeclient::testing::FakeTradeRow;
use harness_lakeclient::BlockingLakeClient;
use harness_lakeclient::LakeClientConfig;
use harness_research::acquisition::lake_fixture_with_subscriptions;
use harness_trading::program::MarketSubscription;
use serde_json::json;
use std::collections::BTreeMap;

const CONTRACT: &str = "SPY240216C00100000";
#[test]
fn both_selectors_feed_quotes_trades_and_bars_through_the_same_program_input() {
    for delta in [false, true] {
        let start = parse_timestamp_ns("2024-01-19T14:30:00Z").unwrap();
        let mut config =
            BacktestConfig::new("option-data", start, start + 120_000_000_000, 1).unwrap();
        config.shell = "study".into();
        config.symbols = vec!["ATM".into()];
        let mut selector =
            json!({"underlying":"SPY","right":"call","as_of":"2024-01-18T21:00:00Z"});
        if delta {
            selector["days_to_expiration"] = json!(30);
            selector["delta"] = json!("0.50");
        } else {
            selector["expiration"] = json!("2024-02-16");
            selector["strike"] = json!("100");
        }
        config.option_selectors.insert(
            "ATM".into(),
            serde_json::from_value(selector.clone()).unwrap(),
        );
        let server = FakeLakeServer::start(FakeLakeConfig {
            option_resolution: Some(
                json!({"symbol":CONTRACT,"underlying":"SPY","expiration":"2024-02-16","strike":"100","right":"call","multiplier":100,"as_of":"2024-01-18T21:00:00Z","delta":if delta {Some("0.51")}else{None},"quote_time":"2024-01-18T20:59:00Z","method":"test"}),
            ),
            partitions_by_type: BTreeMap::from([
                (
                    "quote".into(),
                    option_quote_parquet(
                        CONTRACT,
                        &[FakeQuoteRow::new(start, "2.3", "2.4", 10, 10, "OPRA")],
                    ),
                ),
                (
                    "trade".into(),
                    option_trade_parquet(
                        CONTRACT,
                        &[FakeTradeRow::new(start, 1, "2.4", 3, "OPRA")],
                    ),
                ),
                (
                    "bars".into(),
                    option_bars_parquet(
                        CONTRACT,
                        &[FakeBarRow::new(start, "2.4", "2.5", "2.3", "2.45", "7")],
                    ),
                ),
            ]),
            ..FakeLakeConfig::new("test")
        });
        let client =
            BlockingLakeClient::new(LakeClientConfig::new(server.base_url(), "test").unwrap())
                .unwrap();
        let inputs: Vec<_> = ["quote", "trade", "bars"]
            .into_iter()
            .map(|kind| MarketSubscription {
                data_type: kind.into(),
                symbols: vec!["ATM".into()],
                interval: (kind == "bars").then(|| "1min".into()),
            })
            .collect();
        let fixture = lake_fixture_with_subscriptions(&config, &client, &inputs).unwrap();
        assert_eq!(
            fixture
                .open_events()
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap()
                .len(),
            3
        );
        assert!(fixture
            .open_events()
            .unwrap()
            .all(|e| e.unwrap().symbol() == "ATM"));
        assert!(fixture.source_id.contains(CONTRACT));
        assert!(fixture.source_id.contains("2024-01-18T21:00:00Z"));
        let requests = server.requests();
        assert_eq!(
            requests
                .iter()
                .filter(|r| r.path == "/v1/options/resolve")
                .count(),
            1
        );
        for request in requests.iter().filter(|r| r.path == "/v1/catalog/ensure") {
            let body: serde_json::Value =
                serde_json::from_slice(request.body.as_ref().unwrap()).unwrap();
            assert_eq!(body["asset_class"], "option");
            assert_eq!(body["symbol"], CONTRACT);
        }
        for request in requests.iter().filter(|r| r.path == "/v1/snapshot") {
            assert_eq!(request.query["asset_class"], "option");
            assert_eq!(request.query["symbol"], CONTRACT);
        }
        let mut future = config;
        future.option_selectors.get_mut("ATM").unwrap().as_of = "2024-01-19T15:00:00Z".into();
        server.clear_requests();
        assert!(lake_fixture_with_subscriptions(&future, &client, &inputs).is_err());
        assert!(server.requests().is_empty());
    }
}

#[test]
fn bare_contract_symbols_are_classified_as_options() {
    let start = parse_timestamp_ns("2024-01-19T14:30:00Z").unwrap();
    let mut config =
        BacktestConfig::new("option-symbol", start, start + 120_000_000_000, 1).unwrap();
    config.shell = "study".into();
    config.symbols = vec![CONTRACT.into()];
    let server = FakeLakeServer::start(FakeLakeConfig {
        partitions_by_type: BTreeMap::from([(
            "quote".into(),
            option_quote_parquet(
                CONTRACT,
                &[FakeQuoteRow::new(start, "2.3", "2.4", 10, 10, "OPRA")],
            ),
        )]),
        ..FakeLakeConfig::new("test")
    });
    let client =
        BlockingLakeClient::new(LakeClientConfig::new(server.base_url(), "test").unwrap()).unwrap();
    let inputs = vec![MarketSubscription {
        data_type: "quote".into(),
        symbols: vec![CONTRACT.into()],
        interval: None,
    }];
    assert_eq!(
        lake_fixture_with_subscriptions(&config, &client, &inputs)
            .unwrap()
            .open_events()
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap()
            .len(),
        1
    );
    let request = server
        .requests()
        .into_iter()
        .find(|r| r.path == "/v1/catalog/ensure")
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(request.body.as_ref().unwrap()).unwrap();
    assert_eq!(body["asset_class"], "option");
}
