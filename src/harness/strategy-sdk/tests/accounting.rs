//! Author-facing mechanics: partial fills, return units and explicit definitions.
use harness_strategy_sdk::building_blocks::cash_return_bps;
use harness_strategy_sdk::building_blocks::empirical_quantile;
use harness_strategy_sdk::building_blocks::haar_random_walk_energy_share;
use harness_strategy_sdk::building_blocks::log_price_slope_bps;
use harness_strategy_sdk::building_blocks::FillLedger;
use harness_strategy_sdk::building_blocks::PendingOrder;
use harness_strategy_sdk::Decimal;
use harness_strategy_sdk::Fill;

fn d(value: &str) -> Decimal {
    value.parse().unwrap()
}

fn fill<'a>(id: &'a str, order: &'a str, side: &'a str, quantity: &str, price: &str) -> Fill<'a> {
    Fill {
        event_id: id,
        timestamp_ns: 1,
        sequence: 1,
        fill_id: id,
        order_id: order,
        symbol: "MSFT",
        side,
        quantity: d(quantity),
        price: d(price),
        fee: d("0.1"),
        liquidity: "taker",
    }
}

#[test]
fn partial_entries_and_exits_reconcile_inventory_cash_and_fees() {
    let mut ledger = FillLedger::new("MSFT");
    let first = fill("1", "entry", "buy", "4", "100");
    assert_eq!(ledger.apply(&first), Ok(true));
    assert_eq!(ledger.apply(&first), Ok(false));
    ledger
        .apply(&fill("2", "entry", "buy", "6", "101"))
        .unwrap();
    assert_eq!(ledger.quantity(), d("10"));
    assert_eq!(ledger.filled_quantity("entry"), d("10"));
    ledger
        .apply(&fill("3", "exit", "sell", "3", "102"))
        .unwrap();
    assert_eq!(ledger.quantity(), d("7"));
    assert_eq!(ledger.net_pnl(d("102")).unwrap(), d("13.7"));
    ledger
        .apply(&fill("4", "exit", "sell", "7", "102"))
        .unwrap();
    assert_eq!(ledger.quantity(), Decimal::ZERO);
    assert_eq!(ledger.cash_flow(), d("14"));
    assert_eq!(ledger.fees(), d("0.4"));
    assert_eq!(ledger.net_pnl(d("102")).unwrap(), d("13.6"));
    let before = ledger.net_pnl(d("102")).unwrap();
    assert!(ledger
        .apply(&fill("1", "entry", "buy", "5", "100"))
        .is_err());
    assert_eq!(ledger.net_pnl(d("102")).unwrap(), before);
}

#[test]
fn first_partial_fill_never_completes_an_entry_or_exit() {
    let mut ledger = FillLedger::new("MSFT");
    let entry = PendingOrder::new("entry", d("121")).unwrap();
    ledger
        .apply(&fill("1", "entry", "sell", "4", "100"))
        .unwrap();
    assert!(!entry.is_filled(&ledger));
    assert_eq!(entry.remaining(&ledger).unwrap(), d("117"));
    ledger
        .apply(&fill("2", "entry", "sell", "45", "101"))
        .unwrap();
    ledger
        .apply(&fill("3", "entry", "sell", "72", "102"))
        .unwrap();
    assert!(entry.is_filled(&ledger));
    assert_eq!(ledger.quantity(), d("-121"));
    assert_eq!(
        ledger.average_fill_price("entry").unwrap(),
        d("12289") / d("121")
    );
    let exit = PendingOrder::new("exit", ledger.quantity().abs()).unwrap();
    ledger.apply(&fill("4", "exit", "buy", "4", "99")).unwrap();
    assert!(!exit.is_filled(&ledger));
    assert_eq!(ledger.quantity(), d("-117"));
    ledger
        .apply(&fill("5", "exit", "buy", "117", "99"))
        .unwrap();
    assert!(exit.is_filled(&ledger));
    assert_eq!(ledger.quantity(), Decimal::ZERO);
    assert_eq!(ledger.net_pnl(d("99")).unwrap(), d("309.5"));
}

#[test]
fn shorts_and_reversals_keep_actual_signed_inventory() {
    let mut ledger = FillLedger::new("MSFT");
    ledger
        .apply(&fill("1", "short", "sell", "10", "100"))
        .unwrap();
    assert_eq!(ledger.quantity(), d("-10"));
    ledger
        .apply(&fill("2", "reverse", "buy", "15", "99"))
        .unwrap();
    assert_eq!(ledger.quantity(), d("5"));
    assert_eq!(ledger.net_pnl(d("99")).unwrap(), d("9.8"));
    let mut invalid = fill("3", "bad", "invalid", "2", "99");
    assert!(ledger.apply(&invalid).is_err());
    invalid.side = "buy";
    invalid.symbol = "OTHER";
    assert!(ledger.apply(&invalid).is_err());
    assert_eq!(ledger.quantity(), d("5"));
}

#[test]
fn returns_use_entry_notional_for_long_and_short_quantities() {
    assert!((cash_return_bps(d("10"), d("10"), d("100")).unwrap() - 100.0).abs() < 1e-12);
    assert!((cash_return_bps(d("-10"), d("-10"), d("100")).unwrap() + 100.0).abs() < 1e-12);
    assert!(cash_return_bps(d("10"), Decimal::ZERO, d("100")).is_err());
}

#[test]
fn definitions_are_distinct_from_absolute_cutoffs_and_endpoint_momentum() {
    let sample = [0.95, 0.8, 0.9, 0.85];
    assert_eq!(empirical_quantile(&sample, 0.3).unwrap(), 0.85);
    assert_eq!(empirical_quantile(&sample, 0.0).unwrap(), 0.8);
    assert_eq!(empirical_quantile(&sample, 1.0).unwrap(), 0.95);
    assert!(empirical_quantile(&[f64::NAN], 0.3).is_err());
    assert!(empirical_quantile(&sample, 1.1).is_err());
    let prices = [1.0, 2.0_f64.exp(), 1.0, 1.0];
    assert!((log_price_slope_bps(&prices).unwrap() + 2000.0).abs() < 1e-10);
    assert!(log_price_slope_bps(&[0.0, 1.0]).is_err());
    assert!((haar_random_walk_energy_share(32, 8).unwrap() - 150.5 / 170.5).abs() < 1e-14);
    assert_eq!(haar_random_walk_energy_share(32, 2).unwrap(), 1.0);
    assert!(haar_random_walk_energy_share(30, 8).is_err());
}
