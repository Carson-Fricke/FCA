use super::*;

fn point(timestamp_ns: i64, equity: &str) -> EquityPoint {
    EquityPoint {
        timestamp_ns,
        equity: dec(equity),
    }
}

#[test]
fn total_return_and_drawdown() {
    let points = [point(0, "100"), point(10, "150"), point(20, "120")];
    assert_eq!(total_return(&points), Some(dec("0.2")));
    // Peak 150 to trough 120 => 1 - 120/150 = 0.2
    assert_eq!(max_drawdown(&points), dec("0.2"));
    assert_eq!(longest_drawdown(&points).duration_ns, 10);
}

#[test]
fn single_point_curve_is_undefined() {
    let points = [point(0, "100")];
    assert_eq!(total_return(&points), None);
    assert_eq!(cagr(&points), None);
    assert_eq!(max_drawdown(&points), Decimal18::ZERO);
}

#[test]
fn trade_stats_classify_wins_and_losses() {
    let trades = vec![
        TradeRow {
            trade_id: "a".to_string(),
            strategy_id: "s".to_string(),
            realized_pnl: dec("10"),
        },
        TradeRow {
            trade_id: "b".to_string(),
            strategy_id: "s".to_string(),
            realized_pnl: dec("-4"),
        },
        TradeRow {
            trade_id: "c".to_string(),
            strategy_id: "s".to_string(),
            realized_pnl: Decimal18::ZERO,
        },
    ];
    let stats = trade_stats(&trades);
    assert_eq!(stats.trade_count, 3);
    assert_eq!(stats.win_count, 1);
    assert_eq!(stats.loss_count, 1);
    assert_eq!(stats.gross_profit, dec("10"));
    assert_eq!(stats.gross_loss, dec("4"));
    assert_eq!(stats.profit_factor, Some(dec("2.5")));
    assert_eq!(stats.largest_win, Some(dec("10")));
    assert_eq!(stats.largest_loss, Some(dec("-4")));
}

#[test]
fn outcome_counts_classify_by_exact_string() {
    let orders = vec![
        OrderRow {
            order_id: "1".to_string(),
            strategy_id: "s".to_string(),
            outcome: "filled".to_string(),
        },
        OrderRow {
            order_id: "2".to_string(),
            strategy_id: "s".to_string(),
            outcome: "FILLED".to_string(),
        },
    ];
    let counts = order_outcome_counts(&orders);
    assert_eq!(counts.filled, 1);
    assert_eq!(counts.other, 1);
}

#[test]
fn per_strategy_attribution_unions_the_ledgers() {
    let fills = vec![FillRow {
        fill_id: "f".to_string(),
        order_id: "o".to_string(),
        strategy_id: "a".to_string(),
        quantity: dec("1"),
        price: dec("10"),
        fee: dec("0.5"),
        slippage: dec("0.25"),
        latency_ns: Some(1000),
    }];
    let orders = vec![OrderRow {
        order_id: "o".to_string(),
        strategy_id: "b".to_string(),
        outcome: "rejected".to_string(),
    }];
    let attribution = per_strategy_attribution(&fills, &orders, &[]);
    assert_eq!(attribution.len(), 2);
    assert_eq!(attribution["a"].execution.fees_total, dec("0.5"));
    assert_eq!(attribution["b"].execution.outcomes.rejected, 1);
    assert_eq!(attribution["b"].execution.fill_count, 0);
}

#[test]
fn sharpe_is_none_without_variance() {
    assert_eq!(sharpe(&[dec("1"), dec("1")], 252), None);
    assert!(sharpe(&[dec("1"), dec("2"), dec("3")], 252).is_some());
}
