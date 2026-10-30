//! A real authored strategy uses context positions/orders and matches published accounting.
use backtest::backtest::{
    config::{BacktestConfig, FeeBasis, FeeRule, StrategyInstanceConfig},
    engine_core::EngineOptions,
};
use harness_contracts::canonical_json::JsonValue;
use harness_lakeclient::{
    testing::{quote_parquet, FakeLakeConfig, FakeLakeServer, FakeQuoteRow},
    BlockingLakeClient, LakeClientConfig,
};
use harness_research::build_instances;
use harness_research::replay::run_native_job_with_lake_client;
use harness_strategy_jit::ManagedWorkspace;
use num::Decimal;
use serde_json::{json, Value};
use std::collections::BTreeMap;

#[test]
fn authored_managed_positions_and_all_four_order_types_use_authoritative_accounting() {
    let directory = tempfile::tempdir().unwrap();
    let workspace = ManagedWorkspace::open(directory.path()).unwrap();
    let source = include_str!("fixtures/managed_strategy.rs");
    let mut config = BacktestConfig::from_yaml("shell: backtest\njob_id: sdk-managed\nstart: 2024-01-02T14:30:00Z\nend: 2024-01-02T15:00:00Z\ndata:\n  symbols: [SPY]\n  subscriptions:\n    - data_type: quote\n      symbols: [SPY]\n").unwrap();
    config.fees.rules.push(FeeRule {
        fee_id: "commission".into(),
        basis: FeeBasis::PerUnit,
        rate: "0.1".parse().unwrap(),
        minimum: Decimal::ZERO,
        side: "both".into(),
    });
    let quotes: Vec<_> = (0..10)
        .map(|i| {
            FakeQuoteRow::new(
                config.start_ns + i * 60_000_000_000,
                if i < 3 { "99" } else { "102" },
                if i < 3 { "100" } else { "103" },
                4,
                4,
                "XNYS",
            )
        })
        .collect();
    let server = FakeLakeServer::start(FakeLakeConfig {
        partitions_by_type: BTreeMap::from([("quote".into(), quote_parquet("SPY", &quotes))]),
        ..FakeLakeConfig::new("managed-token")
    });
    let client =
        BlockingLakeClient::new(LakeClientConfig::new(server.base_url(), "managed-token").unwrap())
            .unwrap();
    for (name, parameters) in [
        ("long", json!({"direction":1})),
        ("short", json!({"direction":-1})),
        ("types", json!({"order_types":true})),
        ("cancel", json!({"cancel_order":true})),
    ] {
        let mut resolved = BTreeMap::from([("source".into(), JsonValue::string(source))]);
        for (key, value) in parameters.as_object().unwrap() {
            resolved.insert(
                key.clone(),
                match value {
                    Value::Bool(v) => JsonValue::Bool(*v),
                    Value::Number(v) => JsonValue::Int(v.as_i64().unwrap()),
                    _ => panic!("unexpected fixture parameter"),
                },
            );
        }
        let bundle_hash = harness_contracts::sha256::digest_hex(source.as_bytes());
        config.strategy_instances = vec![StrategyInstanceConfig {
            instance_id: "managed".into(),
            implementation: "jit".into(),
            bundle_hash: bundle_hash.clone(),
            parameters: resolved,
            symbols: vec!["SPY".into()],
            capital_fraction: "1".into(),
        }];
        let instances = build_instances(&config, &workspace).unwrap();
        let output = directory.path().join(name);
        let (outcome, _) = run_native_job_with_lake_client(
            &config,
            &EngineOptions::default(),
            Some(&output),
            instances,
            &client,
        )
        .unwrap();
        assert!(outcome.completed());
        assert_eq!(
            outcome.custom_metrics["sdk_order_count"],
            outcome.orders.len().to_string()
        );
        assert_eq!(outcome.manifest.strategies[0].bundle_hash, bundle_hash);
        assert!(!outcome.manifest.strategies[0].parameter_hash.is_empty());
        let metrics: Value =
            serde_json::from_slice(&std::fs::read(output.join("metrics.json")).unwrap()).unwrap();
        let capital = &metrics["capital"];
        let initial: Decimal = capital["initial_cash"].as_str().unwrap().parse().unwrap();
        assert_eq!(initial, Decimal::from_i128(1_000_000));
        let peak: Decimal = capital["peak_gross_exposure"]
            .as_str()
            .unwrap()
            .parse()
            .unwrap();
        if name == "cancel" {
            assert!(outcome.fills.is_empty());
            assert_eq!(outcome.orders.len(), 1);
            assert_eq!(outcome.orders[0].status, "canceled");
            assert_eq!(peak, Decimal::ZERO);
            continue;
        }
        assert!(peak > Decimal::ZERO && peak < Decimal::from_i128(1100));
        if name == "types" {
            assert_eq!(outcome.orders.len(), 4);
            assert!(outcome.orders.iter().all(|o| o.status == "filled"));
            let ids: BTreeMap<_, _> = outcome.orders.iter().map(|o| (&o.order_id, o)).collect();
            assert_eq!(ids.len(), 4, "SDK allocates unique host-owned IDs");
        } else {
            assert_eq!(outcome.orders.len(), 2);
            assert_eq!(outcome.fills.len(), 6);
            let realized: Decimal = outcome.custom_metrics["sdk_realized_pnl"].parse().unwrap();
            let reported: Decimal = metrics["trades"]["net_pnl"]
                .as_str()
                .unwrap()
                .parse()
                .unwrap();
            assert_eq!(realized, reported);
            if name == "long" {
                assert_eq!(realized, Decimal::from_i128(18));
            }
            assert!(realized.abs() < Decimal::from_i128(100));
            for row in metrics["trades"]["rows"].as_array().unwrap() {
                let bps: Decimal = row["net_return_bps"].as_str().unwrap().parse().unwrap();
                assert!(
                    bps.abs() < Decimal::from_i128(1000),
                    "entry principal never masquerades as return"
                );
                if name == "long" {
                    assert_eq!(bps, Decimal::from_i128(180));
                }
            }
        }
    }
}
