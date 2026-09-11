use super::*;

const DAY: i64 = NANOS_PER_DAY;

fn pt(timestamp_ns: i64, equity: i128) -> EquityPoint {
    EquityPoint {
        timestamp_ns,
        equity,
    }
}

fn trade(pnl: i128) -> TradeRow {
    TradeRow { realized_pnl: pnl }
}

fn fill(latency_ns: Option<i64>, fee: i128, slippage: i128) -> FillRow {
    FillRow {
        quantity: dec_text("1"),
        price: dec_text("100"),
        fee,
        slippage,
        latency_ns,
    }
}

#[test]
fn dec_text_parses_signs_integer_and_fractional_parts() {
    assert_eq!(dec_text("1"), ONE);
    assert_eq!(dec_text("-1"), -ONE);
    assert_eq!(dec_text("0.4"), 400_000_000_000_000_000);
    assert_eq!(dec_text("4.0"), 4 * ONE);
    assert_eq!(dec_text("0"), 0);
}

#[test]
fn total_return_and_cagr_match_the_hand_computed_vectors() {
    let curve = [
        pt(0, dec_text("100")),
        pt(DAY, dec_text("80")),
        pt(2 * DAY, dec_text("121")),
    ];
    assert_eq!(total_return(&curve).unwrap(), Some(dec_text("0.21")));
    assert_eq!(total_return(&[pt(0, dec_text("100"))]).unwrap(), None);
    assert_eq!(
        total_return(&[pt(0, dec_text("0")), pt(DAY, dec_text("50"))]).unwrap(),
        None
    );
    assert_eq!(total_return(&[]).unwrap(), None);

    let year = [pt(0, dec_text("100")), pt(365 * DAY, dec_text("121"))];
    assert!((cagr(&year).unwrap().unwrap() - 1.21).abs() < 1e-12);
    let half = [pt(0, dec_text("100")), pt(365 * DAY / 2, dec_text("121"))];
    assert!((cagr(&half).unwrap().unwrap() - 1.21 * 1.21).abs() < 1e-12);
    assert_eq!(
        cagr(&[pt(0, dec_text("100")), pt(DAY, dec_text("0"))]).unwrap(),
        None
    );
}

#[test]
fn drawdown_metrics_pin_the_hand_computed_vectors() {
    let curve = [
        pt(0, dec_text("100")),
        pt(DAY, dec_text("80")),
        pt(2 * DAY, dec_text("120")),
        pt(3 * DAY, dec_text("90")),
    ];
    assert_eq!(max_drawdown(&curve).unwrap(), dec_text("0.25"));
    assert_eq!(
        max_drawdown(&[pt(0, dec_text("100")), pt(DAY, dec_text("120"))]).unwrap(),
        dec_text("0")
    );
    assert_eq!(
        max_drawdown(&[pt(0, dec_text("100")), pt(DAY, dec_text("0"))]).unwrap(),
        dec_text("1")
    );
    assert!(max_drawdown(&[pt(0, dec_text("100")), pt(DAY, dec_text("-1"))]).is_err());

    let span = longest_drawdown(&curve).unwrap();
    assert_eq!(span.duration_ns, 2 * DAY);
    assert!(span.recovered);
    let open = [
        pt(0, dec_text("100")),
        pt(DAY, dec_text("50")),
        pt(2 * DAY, dec_text("0")),
    ];
    let span = longest_drawdown(&open).unwrap();
    assert_eq!(span.duration_ns, 2 * DAY);
    assert!(!span.recovered);
    let bundled = equity_curve_metrics(&curve).unwrap();
    assert_eq!(bundled.total_return, total_return(&curve).unwrap());
    assert_eq!(bundled.max_drawdown, max_drawdown(&curve).unwrap());
}

#[test]
fn scale18_square_root_rounds_half_even_against_exact_squares() {
    assert_eq!(dec_sqrt(dec_text("0.09")).unwrap(), 300_000_000_000_000_000);
    assert_eq!(dec_sqrt(ONE).unwrap(), ONE);
    assert_eq!(dec_sqrt(0).unwrap(), 0);
    assert_eq!(dec_sqrt(-1), Err(MetricsError::InvalidInput));
    assert_eq!(
        dec_sqrt(dec_text("2.25")).unwrap(),
        1_500_000_000_000_000_000
    );
}

#[test]
fn sharpe_and_sortino_pin_the_hand_computed_vectors() {
    let returns = [
        dec_text("0.05"),
        dec_text("0.05"),
        dec_text("-0.01"),
        dec_text("-0.01"),
    ];
    let expected = (2.0 / 3.0) * 252.0f64.sqrt();
    assert!((sharpe(&returns, 252).unwrap().unwrap() - expected).abs() < 1e-12);
    assert_eq!(
        sharpe(&[dec_text("0.02"), dec_text("0.02")], 252).unwrap(),
        None
    );
    assert_eq!(sharpe(&[dec_text("0.02")], 252).unwrap(), None);

    let returns = [
        dec_text("0.03"),
        dec_text("-0.01"),
        dec_text("0.05"),
        dec_text("-0.03"),
    ];
    let downside = ((0.0001 + 0.0009) / 4.0f64).sqrt();
    assert!((sortino(&returns, 4, 0).unwrap().unwrap() - 0.01 / downside * 2.0).abs() < 1e-12);
    assert_eq!(
        sortino(&[dec_text("0.01"), dec_text("0.02")], 4, 0).unwrap(),
        None
    );
}

#[test]
fn trade_stats_pin_the_five_trade_vector() {
    let trades = [
        trade(dec_text("100")),
        trade(dec_text("-50")),
        trade(dec_text("30")),
        trade(dec_text("-20")),
        trade(dec_text("0")),
    ];
    let stats = trade_stats(&trades).unwrap();
    assert_eq!(stats.trade_count, 5);
    assert_eq!(stats.win_count, 2);
    assert_eq!(stats.loss_count, 2);
    assert_eq!(stats.hit_rate, Some(dec_text("0.4")));
    assert_eq!(stats.total_pnl, dec_text("60"));
    assert_eq!(stats.gross_profit, dec_text("130"));
    assert_eq!(stats.gross_loss, dec_text("70"));
    assert_eq!(stats.profit_factor, Some(1_857_142_857_142_857_143));
    assert_eq!(stats.average_win, Some(dec_text("65")));
    assert_eq!(stats.average_loss, Some(dec_text("-35")));
    assert_eq!(stats.largest_win, Some(dec_text("100")));
    assert_eq!(stats.largest_loss, Some(dec_text("-50")));

    let no_loss = [trade(dec_text("10")), trade(dec_text("5"))];
    let stats = trade_stats(&no_loss).unwrap();
    assert_eq!(stats.profit_factor, None);
    assert_eq!(stats.hit_rate, Some(ONE));
    let empty = trade_stats(&[]).unwrap();
    assert_eq!(empty.trade_count, 0);
    assert_eq!(empty.hit_rate, None);
}

#[test]
fn execution_stats_pin_the_hand_computed_vectors() {
    assert_eq!(
        fill_slippage(dec_text("2"), dec_text("100"), dec_text("100.5")).unwrap(),
        dec_text("1.0")
    );
    assert_eq!(
        fill_slippage(dec_text("-3"), dec_text("200"), dec_text("199.5")).unwrap(),
        dec_text("1.5")
    );

    let fills = [
        fill(Some(100), 0, 0),
        fill(Some(200), 0, 0),
        fill(None, 0, 0),
        fill(Some(300), 0, 0),
    ];
    let stats = latency_stats(&fills).unwrap();
    assert_eq!(stats.count, 3);
    assert_eq!(stats.mean_ns, Some(dec_text("200")));
    assert_eq!(stats.max_ns, Some(300));
    assert_eq!(latency_stats(&[]).unwrap().mean_ns, None);

    let orders = [
        OrderRow { outcome: "filled" },
        OrderRow { outcome: "filled" },
        OrderRow {
            outcome: "rejected",
        },
        OrderRow {
            outcome: "cancelled",
        },
        OrderRow { outcome: "expired" },
    ];
    let counts = order_outcome_counts(&orders);
    assert_eq!(counts.total, 5);
    assert_eq!(counts.filled, 2);
    assert_eq!(counts.rejected, 1);
    assert_eq!(counts.cancelled, 1);
    assert_eq!(counts.other, 1);

    let fills = [
        fill(Some(10), dec_text("1.5"), dec_text("1.0")),
        fill(Some(30), dec_text("2.5"), dec_text("0.2")),
    ];
    let orders = [
        OrderRow { outcome: "filled" },
        OrderRow {
            outcome: "rejected",
        },
    ];
    let stats = execution_stats(&fills, &orders).unwrap();
    assert_eq!(stats.fill_count, 2);
    assert_eq!(stats.fees_total, dec_text("4.0"));
    assert_eq!(stats.slippage_total, dec_text("1.2"));
    assert_eq!(stats.latency.mean_ns, Some(dec_text("20")));
    assert_eq!(stats.outcomes.total, 2);
}

#[test]
fn per_strategy_attribution_groups_and_zeroes_absent_strategies() {
    let fills = [
        AnnotatedFill {
            strategy_id: "alpha",
            fill: fill(Some(10), dec_text("1"), dec_text("0.5")),
        },
        AnnotatedFill {
            strategy_id: "beta",
            fill: fill(Some(20), dec_text("2"), dec_text("0")),
        },
    ];
    let orders = [AnnotatedOrder {
        strategy_id: "alpha",
        order: OrderRow { outcome: "filled" },
    }];
    let trades = [AnnotatedTrade {
        strategy_id: "alpha",
        trade: trade(dec_text("10")),
    }];
    let attribution = per_strategy_attribution(&fills, &orders, &trades).unwrap();
    assert_eq!(attribution.len(), 2);
    assert_eq!(attribution[0].strategy_id, "alpha");
    assert_eq!(attribution[1].strategy_id, "beta");
    assert_eq!(attribution[0].trades.total_pnl, dec_text("10"));
    assert_eq!(attribution[0].execution.fees_total, dec_text("1"));
    assert_eq!(attribution[0].execution.slippage_total, dec_text("0.5"));
    assert_eq!(attribution[0].execution.outcomes.filled, 1);
    assert_eq!(attribution[1].trades.trade_count, 0);
    assert_eq!(attribution[1].execution.fees_total, dec_text("2"));
    assert_eq!(attribution[1].execution.outcomes.total, 0);
    assert!(per_strategy_attribution(&[], &[], &[]).unwrap().is_empty());
}
