//! Numeric and textual callers share one current Arrow schema and exact artifact bytes.
use backtest::results::recorder::{
    EquityRecord, PortfolioEquityRecord, RecorderConfig, RunRecorder,
};
use num::Decimal18;

#[test]
fn typed_equity_preserves_ids_order_decimal_text_and_parquet_bytes_across_flushes() {
    let root = tempfile::tempdir().unwrap();
    let config = |name: &str| {
        RecorderConfig::new("same-job", root.path().join(name))
            .unwrap()
            .with_seed(7i64)
            .with_account_id("backtest")
            .unwrap()
    };
    let mut typed = RunRecorder::new(config("typed")).unwrap();
    let mut text = RunRecorder::new(config("text")).unwrap();
    for i in 0..8193i64 {
        let cash = Decimal18::from_i64(10000 - i % 7);
        let market_value = Decimal18::from_i64(i % 7);
        let equity = Decimal18::from_i64(10000);
        typed
            .record_portfolio_equity(&PortfolioEquityRecord {
                timestamp_ns: i,
                cash,
                equity,
                market_value,
                realized_pnl: Decimal18::ZERO,
                unrealized_pnl: Decimal18::ZERO,
                total_pnl: Decimal18::ZERO,
                position_count: 1,
                fill_count: 3,
            })
            .unwrap();
        text.record_equity(&EquityRecord {
            at_ns: Some(i),
            cash: cash.to_string(),
            equity: Some(equity.to_string()),
            market_value: Some(market_value.to_string()),
            realized_pnl: Decimal18::ZERO.to_string(),
            unrealized_pnl: Some(Decimal18::ZERO.to_string()),
            total_pnl: Some(Decimal18::ZERO.to_string()),
            source: "portfolio".into(),
            account_id: None,
            book_id: None,
            snapshot_id: None,
            position_count: 1,
            fill_count: 3,
        })
        .unwrap();
    }
    let left = typed.close().unwrap();
    let right = text.close().unwrap();
    assert_eq!(left.run_digest, right.run_digest);
    assert_eq!(
        std::fs::read(root.path().join("typed/equity.parquet")).unwrap(),
        std::fs::read(root.path().join("text/equity.parquet")).unwrap()
    );
    assert!(typed
        .record_portfolio_equity(&PortfolioEquityRecord {
            timestamp_ns: 0,
            cash: Decimal18::ZERO,
            equity: Decimal18::ZERO,
            market_value: Decimal18::ZERO,
            realized_pnl: Decimal18::ZERO,
            unrealized_pnl: Decimal18::ZERO,
            total_pnl: Decimal18::ZERO,
            position_count: 0,
            fill_count: 0
        })
        .is_err());
}
