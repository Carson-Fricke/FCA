//! Loaded ABI declarations and exact mixed-input dispatch without engine filters.
use harness_strategy_jit::JitStrategyRuntime;
use harness_strategy_jit::JitStudyRuntime;
use harness_strategy_jit::ManagedWorkspace;
use harness_strategy_sdk::serde_json::json;
use harness_strategy_sdk::Decimal;
use harness_trading::program::EngineBar;
use harness_trading::program::EngineBarFrame;
use harness_trading::program::EngineMarket;
use harness_trading::program::StrategyRuntime;

fn quote(symbol: &str) -> EngineMarket {
    EngineMarket::Quote {
        event_id: "quote".into(),
        timestamp_ns: 10,
        sequence: 1,
        symbol: symbol.into(),
        bid: Decimal::ZERO,
        ask: Decimal::ZERO,
        bid_quantity: Decimal::ZERO,
        ask_quantity: Decimal::ZERO,
        venue: "test".into(),
    }
}
fn trade(symbol: &str) -> EngineMarket {
    EngineMarket::Trade {
        event_id: "trade".into(),
        timestamp_ns: 10,
        sequence: 1,
        symbol: symbol.into(),
        price: Decimal::ZERO,
        quantity: Decimal::ZERO,
        venue: "test".into(),
        trade_id: "trade".into(),
    }
}
fn frame(interval: &str) -> EngineBarFrame {
    let bar = |symbol: &str| EngineBar {
        event_id: "bar".into(),
        timestamp_ns: 10,
        sequence: 1,
        symbol: symbol.into(),
        open: Decimal::ZERO,
        high: Decimal::ZERO,
        low: Decimal::ZERO,
        close: Decimal::ZERO,
        volume: Decimal::ZERO,
    };
    EngineBarFrame {
        interval: interval.into(),
        interval_start_ns: 10,
        interval_end_ns: 20,
        available_at_ns: 25,
        bars_by_symbol: [("AAA".into(), bar("AAA")), ("OTHER".into(), bar("OTHER"))].into(),
        missing_symbols: vec![],
    }
}

#[test]
fn study_strategy_and_owned_indicators_share_filtered_callbacks_and_effective_requirements() {
    let directory = std::env::temp_dir().join(format!(
        "harness-mixed-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&directory).unwrap();
    let workspace = ManagedWorkspace::open(&directory).unwrap();
    let mut study = JitStudyRuntime::from_source(
        &workspace,
        include_str!("fixtures/mixed_study.rs"),
        &json!({}),
    )
    .unwrap();
    let requirements = study.subscriptions().unwrap();
    assert_eq!(requirements.len(), 5);
    assert_eq!(
        requirements
            .iter()
            .find(|input| input.data_type == "quote")
            .unwrap()
            .symbols,
        ["AAA", "DEP"]
    );
    for event in [
        quote("DEP"),
        quote("AAA"),
        quote("OTHER"),
        trade("BBB"),
        trade("AAA"),
        trade("DEP"),
    ] {
        assert!(study.on_market(&event).unwrap().is_empty());
    }
    study.on_bar_frame(&frame("1d")).unwrap();
    study.on_bar_frame(&frame("1min")).unwrap();
    study.on_bar_frame(&frame("5min")).unwrap();
    study.on_stop().unwrap();
    let outputs = study.published_outputs();
    assert_eq!(
        outputs["ind/0/in_progress"]["rows"][0][3],
        60_000_000_000_i64
    );
    assert_eq!(
        outputs["ind/0/in_progress"]["rows"][0][4],
        serde_json::Value::Null
    );
    assert_eq!(outputs["ind/0/in_progress"]["rows"][0][8], "1");
    assert_eq!(outputs["ind/0/completed"]["rows"], serde_json::json!([]));
    assert_eq!(outputs["signal"]["kind"], "series");
    assert_eq!(
        outputs["research_view"]["view"]["datasets"][0]["output"],
        "signal"
    );
    assert_eq!(
        study.published_outputs()["signal"]["points"][0]["available_at_ns"],
        25
    );
    for key in ["quotes", "trades", "dependency"] {
        assert_eq!(study.published_metrics()[key], "1");
    }
    assert_eq!(study.published_metrics()["frames"], "3");
    // Actual engine -> ABI -> authored callbacks for all three completed cadences.
    let replay = JitStudyRuntime::from_source(
        &workspace,
        include_str!("fixtures/mixed_study.rs"),
        &json!({}),
    )
    .unwrap();
    let start = backtest::backtest::config::parse_timestamp_ns("2024-01-02T14:30:00Z").unwrap();
    let end = start + 390 * 60_000_000_000;
    let mut config =
        backtest::backtest::config::BacktestConfig::new("multi-jit", start, end, 0).unwrap();
    config.shell = "study".into();
    config.symbols = vec!["AAA".into(), "BBB".into(), "DEP".into()];
    config.data_subscriptions = replay.subscriptions().unwrap();
    config.data_subscriptions_declared = true;
    use backtest::backtest::fixture::{
        ExchangeSession, MarketEvent, ReplayFingerprints, ReplayFixture, SyntheticDataOptions,
    };
    let mut events = vec![];
    for symbol in ["AAA", "DEP"] {
        events.push(MarketEvent::Quote {
            event_id: format!("quote-{symbol}"),
            timestamp_ns: start + 1,
            sequence: 1,
            symbol: symbol.into(),
            bid: num::Decimal18::from_i64(10),
            ask: num::Decimal18::from_i64(11),
            bid_quantity: num::Decimal18::from_i64(1),
            ask_quantity: num::Decimal18::from_i64(1),
            venue: "test".into(),
        });
    }
    events.push(MarketEvent::Trade {
        event_id: "trade-BBB".into(),
        timestamp_ns: start + 1,
        sequence: 2,
        symbol: "BBB".into(),
        price: num::Decimal18::from_i64(10),
        quantity: num::Decimal18::from_i64(1),
        venue: "test".into(),
        trade_id: "trade".into(),
    });
    for minute in 0..390 {
        events.push(MarketEvent::Bar {
            event_id: format!("bar-{minute}"),
            timestamp_ns: start + minute * 60_000_000_000,
            sequence: minute as u64,
            symbol: "AAA".into(),
            open: num::Decimal18::from_i64(10),
            high: num::Decimal18::from_i64(12),
            low: num::Decimal18::from_i64(9),
            close: num::Decimal18::from_i64(11),
            volume: num::Decimal18::from_i64(100),
        });
    }
    let fixture = ReplayFixture {
        stream: None,
        fingerprints: ReplayFingerprints::of_events(&events),
        events,
        sessions: vec![ExchangeSession {
            session_date_days: start.div_euclid(86_400_000_000_000),
            open_ns: start,
            close_ns: end,
        }],
        source_id: "test".into(),
        symbols: config.symbols.clone(),
        expected_data_fingerprint: None,
    };
    let preflight = backtest::backtest::preflight::preflight_data(&config, &fixture).unwrap();
    let manifest =
        backtest::backtest::identity::build_default_manifest(&config, &fixture, &preflight)
            .unwrap();
    let outcome = backtest::backtest::engine_core::run_native_engine(
        &config,
        &fixture,
        &preflight,
        manifest,
        &backtest::backtest::engine_core::EngineOptions::default(),
        vec![harness_trading::program::StrategyInstance {
            instance_id: "study".into(),
            runtime: Box::new(replay),
        }],
        &SyntheticDataOptions::default(),
    )
    .unwrap();
    assert_eq!(outcome.custom_metrics["frames"], "469");
    assert_eq!(outcome.bar_frame_count, 469);
    assert!(outcome.exploratory_outputs["study"]
        .get("ind/0/in_progress")
        .is_some());
    let source = include_str!("fixtures/mixed_strategy.rs");
    let mut strategy = JitStrategyRuntime::from_source(&workspace, source, &json!({})).unwrap();
    let requirements = strategy.subscriptions().unwrap();
    assert_eq!(requirements[2].symbols, ["BBB", "DEP"]);
    assert!(strategy
        .on_bar_frame(&frame("1min"))
        .unwrap_err()
        .to_string()
        .contains("on_frame_orders"));
    for event in [
        trade("DEP"),
        quote("AAA"),
        quote("OTHER"),
        trade("BBB"),
        trade("AAA"),
    ] {
        assert!(strategy.on_market(&event).unwrap().is_empty());
    }
    assert!(strategy.on_frame_orders(&frame("1d")).unwrap().is_empty());
    let orders = strategy.on_frame_orders(&frame("1min")).unwrap();
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].timestamp_ns, 25);
    assert_eq!(orders[0].symbol, "AAA");
    strategy.on_stop().unwrap();
    for key in ["quotes", "trades", "frames", "dependency"] {
        assert_eq!(strategy.published_metrics()[key], "1");
    }
    assert!(
        JitStrategyRuntime::from_source(&workspace, source, &json!({"invalid": true})).is_err()
    );
    for mutation in ["callback", "dependency", "topology"] {
        let mut runtime =
            JitStrategyRuntime::from_source(&workspace, source, &json!({"mutation": mutation}))
                .unwrap();
        let event = if mutation == "dependency" {
            trade("DEP")
        } else {
            quote("AAA")
        };
        let error = runtime.on_market(&event).unwrap_err().to_string();
        assert!(
            error.contains("subscriptions or dependency topology changed"),
            "{mutation}: {error}"
        );
        assert!(
            runtime.on_market(&trade("DEP")).is_err(),
            "failed state must stay rejected"
        );
    }
    let mut runtime =
        JitStrategyRuntime::from_source(&workspace, source, &json!({"mutation": "start"})).unwrap();
    let start = harness_trading::program::StrategyStart {
        instance_id: "test".into(),
        account_id: "test".into(),
        symbols: vec!["AAA".into()],
        seed: 1,
        initial_cash: Decimal::ZERO,
        start_ns: 0,
    };
    assert!(runtime
        .on_start(&start)
        .unwrap_err()
        .to_string()
        .contains("subscriptions or dependency topology changed"));
    assert!(runtime.on_market(&trade("DEP")).is_err());
    drop(runtime);
    let mut invalid = JitStudyRuntime::from_source(
        &workspace,
        include_str!("fixtures/mixed_study.rs"),
        &json!({"invalid_view":true}),
    )
    .unwrap();
    // No dependency event has arrived; the fixture's indicator assertion needs one.
    invalid.on_market(&quote("DEP")).unwrap();
    assert!(invalid
        .on_stop()
        .unwrap_err()
        .to_string()
        .contains("missing research output"));
    assert!(invalid.published_outputs().is_empty());
    drop(invalid);
    drop(study);
    drop(strategy);
    let _ = std::fs::remove_dir_all(directory);
}
