//! Public lake replay reuse preserves native matching, evidence and exact accounts.
use backtest::backtest::{
    config::{parse_timestamp_ns, BacktestConfig},
    engine_core::{run_native_engine, EngineOptions},
    fixture::{
        ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture, SyntheticDataOptions,
    },
    identity::build_default_manifest,
    preflight::preflight_data,
};
use harness_lakeclient::{
    testing::{
        quote_parquet, trade_parquet, FakeLakeConfig, FakeLakeServer, FakeQuoteRow, FakeTradeRow,
    },
    BlockingLakeClient, LakeClientConfig, ReadSpec,
};
use harness_research::{
    market_replay::{lake_factory_with_cache, LakeInput},
    prepared_replay::PreparedReplayConfig,
};
use harness_trading::{program::*, replay_errors::BacktestError};
use std::collections::BTreeMap;
use std::sync::Arc;

const TOKEN: &str = "prepared-fixture";
fn start() -> i64 {
    parse_timestamp_ns("2024-01-02T14:30:00Z").unwrap()
}
fn inputs() -> Vec<LakeInput> {
    [
        ("quote", marketdata::lake::DataType::Quote),
        ("trade", marketdata::lake::DataType::Trade),
    ]
    .into_iter()
    .map(|(kind, data_type)| LakeInput {
        symbol: "SPY".into(),
        lake_symbol: "SPY".into(),
        data_type,
        read: ReadSpec::new("equity", kind, "SPY", "2024-01-02", "2024-01-02"),
    })
    .collect()
}
fn server(price: &str) -> FakeLakeServer {
    let quotes: Vec<_> = (0..9000)
        .map(|i| FakeQuoteRow::new(start() + i, price, "11.000000000000000001", 2, 2, "XNYS"))
        .collect();
    let trades: Vec<_> = (0..9000)
        .map(|i| FakeTradeRow::new(start() + i, i as u64, "10.000000000000000003", 2, "XNYS"))
        .collect();
    FakeLakeServer::start(FakeLakeConfig {
        partitions_by_type: BTreeMap::from([
            ("quote".into(), quote_parquet("SPY", &quotes)),
            ("trade".into(), trade_parquet("SPY", &trades)),
        ]),
        ..FakeLakeConfig::new(TOKEN)
    })
}
fn client(server: &FakeLakeServer) -> Arc<BlockingLakeClient> {
    Arc::new(
        BlockingLakeClient::new(LakeClientConfig::new(server.base_url(), TOKEN).unwrap()).unwrap(),
    )
}
fn collect(client: Arc<BlockingLakeClient>, cache: PreparedReplayConfig) -> Vec<MarketEvent> {
    lake_factory_with_cache(client, inputs(), start(), start() + 10_000, cache)()
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap()
}
struct Trader {
    quotes: u64,
}
impl StrategyRuntime for Trader {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(
            ["quote", "trade"]
                .map(|kind| MarketSubscription {
                    data_type: kind.into(),
                    symbols: vec!["SPY".into()],
                    interval: None,
                })
                .to_vec(),
        )
    }
    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }
    fn on_market(&mut self, _: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        Ok(vec![])
    }
    fn on_market_actions(
        &mut self,
        event: &EngineMarket,
    ) -> Result<Vec<OrderAction>, BacktestError> {
        if event.data_type() == "quote" {
            self.quotes += 1;
        }
        Ok(vec![OrderAction::TargetPosition {
            symbol: "SPY".into(),
            quantity: num::Decimal::from_i128(if (self.quotes / 1000).is_multiple_of(2) {
                5
            } else {
                -5
            }),
        }])
    }
}
fn replay(events: Vec<MarketEvent>) -> harness_contracts::canonical_json::JsonValue {
    let mut config =
        BacktestConfig::new("prepared-equivalence", start(), start() + 10_000, 7).unwrap();
    config.data_subscriptions = Trader { quotes: 0 }.subscriptions().unwrap();
    config.data_subscriptions_declared = true;
    let fingerprints = ReplayFingerprints::of_events(&events);
    let fixture = ReplayFixture {
        sessions: vec![ExchangeSession {
            session_date_days: 19724,
            open_ns: start(),
            close_ns: start() + 10_000,
        }],
        events,
        stream: None,
        source_id: "prepared-fixture".into(),
        symbols: vec!["SPY".into()],
        fingerprints,
        expected_data_fingerprint: None,
    };
    let preflight = preflight_data(&config, &fixture).unwrap();
    let manifest = build_default_manifest(&config, &fixture, &preflight).unwrap();
    let options = SyntheticDataOptions {
        base_price_scale: 18,
        tick_scale: 18,
        quantity_scale: 18,
        ..Default::default()
    };
    let engine = EngineOptions {
        history: backtest::backtest::history::HistoryResources::new(
            16 * 1024 * 1024,
            Arc::new(tempfile::NamedTempFile::new),
        ),
        ..Default::default()
    };
    let outcome = run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &engine,
        vec![StrategyInstance {
            instance_id: "trader".into(),
            runtime: Box::new(Trader { quotes: 0 }),
        }],
        &options,
    )
    .unwrap();
    assert!(
        outcome.fills.len() > 30,
        "partial fills and flips actually execute"
    );
    outcome.artifacts()
}
#[test]
fn cold_and_cross_client_warm_blocks_match_owned_replay_without_reopening_sources() {
    let directory = tempfile::tempdir().unwrap();
    let cache = PreparedReplayConfig {
        directory: directory.path().into(),
        disk_bytes: 16 * 1024 * 1024,
    };
    let server = server("10.000000000000000002");
    let original_client = client(&server);
    let cold = collect(original_client.clone(), cache.clone());
    assert_eq!(cold.len(), 18_000);
    assert!(cold.iter().all(|e| matches!(e, MarketEvent::Block(_))));
    let owned: Vec<_> = cold.iter().map(MarketEvent::to_owned_event).collect();
    assert_eq!(replay(cold.clone()), replay(owned));
    let object_count = server
        .requests()
        .iter()
        .filter(|r| r.path.starts_with("/objects/"))
        .count();
    assert_eq!(
        std::fs::read_dir(directory.path())
            .unwrap()
            .map(Result::unwrap)
            .filter(|e| e.path().extension().is_some_and(|x| x == "ticks"))
            .count(),
        2
    );
    for input in inputs() {
        for file in original_client.snapshot(&input.read).unwrap().files {
            server.remove_object(&file.checksum);
        }
    }
    let warm = collect(client(&server), cache.clone());
    assert_eq!(warm, cold);
    assert_eq!(replay(warm), replay(cold));
    assert_eq!(
        server
            .requests()
            .iter()
            .filter(|r| r.path.starts_with("/objects/"))
            .count(),
        object_count
    );
    // Integrity failure is reported, never hidden behind an unverified cache hit.
    let file = std::fs::read_dir(directory.path())
        .unwrap()
        .map(Result::unwrap)
        .find(|e| e.path().extension().is_some_and(|x| x == "ticks"))
        .unwrap()
        .path();
    let mut bytes = std::fs::read(&file).unwrap();
    bytes[90] ^= 1;
    std::fs::write(&file, bytes).unwrap();
    let result =
        lake_factory_with_cache(client(&server), inputs(), start(), start() + 10_000, cache)()
            .and_then(|events| events.collect::<Result<Vec<_>, _>>());
    assert!(result.unwrap_err().to_string().contains("checksum"));
}
#[test]
fn changed_pins_and_storage_limits_do_not_reuse_or_truncate_another_input() {
    let directory = tempfile::tempdir().unwrap();
    let cache = PreparedReplayConfig {
        directory: directory.path().into(),
        disk_bytes: 16 * 1024 * 1024,
    };
    let first = collect(client(&server("10")), cache.clone());
    let second = collect(client(&server("9")), cache.clone());
    assert_ne!(replay(first), replay(second));
    let tiny = tempfile::tempdir().unwrap();
    let uncached = collect(
        client(&server("9")),
        PreparedReplayConfig {
            directory: tiny.path().into(),
            disk_bytes: 64,
        },
    );
    assert_eq!(uncached.len(), 18_000);
    assert!(!std::fs::read_dir(tiny.path())
        .unwrap()
        .map(Result::unwrap)
        .any(|e| e.path().extension().is_some_and(|x| x == "ticks")));
}

#[test]
fn interrupted_and_concurrent_cold_consumers_never_deadlock_or_publish_partial_streams() {
    let directory = tempfile::tempdir().unwrap();
    let cache = PreparedReplayConfig {
        directory: directory.path().into(),
        disk_bytes: 16 * 1024 * 1024,
    };
    let server = server("8");
    let client = client(&server);
    let factory = lake_factory_with_cache(client, inputs(), start(), start() + 10_000, cache);
    let mut interrupted = factory().unwrap();
    interrupted.next().unwrap().unwrap();
    drop(interrupted);
    let (send, receive) = std::sync::mpsc::channel();
    let barrier = Arc::new(std::sync::Barrier::new(2));
    for _ in 0..2 {
        let factory = factory.clone();
        let send = send.clone();
        let barrier = barrier.clone();
        std::thread::spawn(move || {
            barrier.wait();
            let result = factory().and_then(|events| events.collect::<Result<Vec<_>, _>>());
            send.send(result).unwrap();
        });
    }
    let first = receive
        .recv_timeout(std::time::Duration::from_secs(10))
        .expect("first replay completes")
        .unwrap();
    let second = receive
        .recv_timeout(std::time::Duration::from_secs(10))
        .expect("second replay completes")
        .unwrap();
    assert_eq!(first.len(), 18_000);
    assert_eq!(first, second);
    let reused = factory().unwrap().collect::<Result<Vec<_>, _>>().unwrap();
    assert_eq!(first, reused);
}
