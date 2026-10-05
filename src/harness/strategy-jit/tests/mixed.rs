//! Loaded ABI declarations and exact mixed-input dispatch without engine filters.
use backtest::backtest::engine_core::{EngineBar, EngineBarFrame, EngineMarket, StrategyRuntime};
use harness_strategy_jit::{JitStrategyRuntime, JitStudyRuntime, ManagedWorkspace};
use harness_strategy_sdk::{serde_json::json, Decimal};

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
    assert_eq!(requirements.len(), 3);
    assert_eq!(requirements[1].symbols, ["AAA", "DEP"]);
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
    study.on_stop().unwrap();
    for key in ["quotes", "trades", "frames", "dependency"] {
        assert_eq!(study.published_metrics()[key], "1");
    }
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
    let start = backtest::backtest::engine_core::StrategyStart {
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
    drop(study);
    drop(strategy);
    let _ = std::fs::remove_dir_all(directory);
}
