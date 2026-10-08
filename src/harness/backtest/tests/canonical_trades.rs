//! Canonical `metrics.json` trade publication from the job ledger.
//!
//! The job derives closed trades from the engine fills with the same FIFO
//! semantics as the Python `run_import._build_trades` trade log and publishes
//! them under the canonical `trades` block. These tests drive a real job
//! through the fake lake routes and assert the published numbers, the
//! undefined profit factor when no trade loses, and the rendered report.

use std::collections::{BTreeMap, VecDeque};

use backtest::backtest::config::{BacktestConfig, FeeBasis, FeeRule};
use backtest::backtest::engine_core::{
    EngineMarket, EngineOptions, EngineOrder, MarketSubscription, StrategyInstance,
    StrategyRuntime, StrategyStart,
};
use backtest::backtest::errors::BacktestError;
use backtest::backtest::job::{parse_timestamp_ns, run_native_job_with_lake_client};
use harness_lakeclient::testing::{quote_stream, FakeLakeConfig, FakeLakeServer, FakeQuoteRow};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};
use serde_json::Value;

const TOKEN: &str = "canonical-trades-token";

/// A runtime that pops one scripted market order per quote event.
struct ScriptedTrader {
    script: VecDeque<(String, i128)>,
    next_id: u64,
}

impl StrategyRuntime for ScriptedTrader {
    fn subscriptions(&self) -> Option<Vec<MarketSubscription>> {
        Some(vec![MarketSubscription {
            data_type: "quote".into(),
            symbols: vec!["SPY".into()],
            interval: None,
        }])
    }

    fn on_start(&mut self, _: &StrategyStart) -> Result<(), BacktestError> {
        Ok(())
    }

    fn on_market(&mut self, event: &EngineMarket) -> Result<Vec<EngineOrder>, BacktestError> {
        let EngineMarket::Quote { timestamp_ns, .. } = event else {
            return Ok(Vec::new());
        };
        let Some((side, quantity)) = self.script.pop_front() else {
            return Ok(Vec::new());
        };
        self.next_id += 1;
        let order_id = format!("order-{}", self.next_id);
        Ok(vec![EngineOrder {
            order_id: order_id.clone(),
            symbol: "SPY".into(),
            side,
            quantity: num::Decimal::from_i128(quantity),
            order_type: "market".into(),
            time_in_force: "day".into(),
            limit_price: None,
            stop_price: None,
            timestamp_ns: *timestamp_ns,
            sequence: 0,
            event_id: format!("event-{order_id}"),
        }])
    }

    fn published_metrics(&self) -> BTreeMap<String, String> {
        BTreeMap::new()
    }
}

fn config() -> BacktestConfig {
    let mut config = BacktestConfig::new(
        "canonical-trades",
        parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start"),
        parse_timestamp_ns("2024-01-02T15:00:00Z").expect("end"),
        7,
    )
    .expect("configuration");
    config.symbols = vec!["SPY".to_string()];
    config
}

fn client(server: &FakeLakeServer) -> BlockingLakeClient {
    BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), TOKEN).expect("lake client config"),
    )
    .expect("blocking lake client")
}

fn quote(timestamp_ns: i64, bid: &str, ask: &str) -> FakeQuoteRow {
    FakeQuoteRow::new(timestamp_ns, bid, ask, 100, 100, "XNYS")
}

/// Run one scripted job and return its `metrics.json` and `report.html`.
fn run_job(name: &str, quotes: &[FakeQuoteRow], script: &[(&str, i128)]) -> (Value, String) {
    run_job_with(name, config(), quotes, script)
}

/// Run one scripted job under the given configuration.
fn run_job_with(
    name: &str,
    config: BacktestConfig,
    quotes: &[FakeQuoteRow],
    script: &[(&str, i128)],
) -> (Value, String) {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([("quote".to_string(), quote_stream("SPY", quotes))]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let directory = std::env::temp_dir().join(format!("backtest-canonical-trades-{name}"));
    let _ = std::fs::remove_dir_all(&directory);
    let (outcome, _) = run_native_job_with_lake_client(
        &config,
        &EngineOptions::default(),
        Some(&directory),
        vec![StrategyInstance {
            instance_id: "trader".into(),
            runtime: Box::new(ScriptedTrader {
                script: script
                    .iter()
                    .map(|(side, quantity)| ((*side).to_string(), *quantity))
                    .collect(),
                next_id: 0,
            }),
        }],
        &client(&server),
    )
    .expect("native job");
    assert!(outcome.completed());
    assert_eq!(outcome.fills.len(), script.len());
    let metrics: Value =
        serde_json::from_slice(&std::fs::read(directory.join("metrics.json")).expect("metrics"))
            .expect("metrics json");
    let report = std::fs::read_to_string(directory.join("report.html")).expect("report html");
    (metrics, report)
}

#[test]
fn publishes_fifo_trade_stats_and_renders_report_with_real_stats() {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let minute = 60_000_000_000;
    // Buy at the first ask (10), sell at the second bid (12): +2.
    // Buy at the third ask (14), sell at the fourth bid (10): -4.
    let quotes = [
        quote(start, "9", "10"),
        quote(start + minute, "12", "13"),
        quote(start + 2 * minute, "13", "14"),
        quote(start + 3 * minute, "10", "11"),
    ];
    let (metrics, report) = run_job(
        "win-loss",
        &quotes,
        &[("buy", 1), ("sell", 1), ("buy", 1), ("sell", 1)],
    );
    let trades = &metrics["trades"];
    assert_eq!(trades["trade_count"], 2);
    assert_eq!(trades["win_count"], 1);
    assert_eq!(trades["loss_count"], 1);
    assert_eq!(trades["hit_rate"], "0.500000000000000000");
    assert_eq!(trades["net_pnl"], "-2.000000000000000000");
    assert_eq!(trades["gross_profit"], "2.000000000000000000");
    assert_eq!(trades["gross_loss"], "4.000000000000000000");
    assert_eq!(trades["profit_factor"], "0.500000000000000000");
    assert_eq!(trades["average_net_win"], "2.000000000000000000");
    assert_eq!(trades["average_net_loss"], "-4.000000000000000000");
    assert_eq!(trades["largest_net_win"], "2.000000000000000000");
    assert_eq!(trades["largest_net_loss"], "-4.000000000000000000");
    assert!(report.contains("<th>Trades</th><td>2</td>"));
    assert!(report.contains("<th>Wins</th><td>1</td>"));
    assert!(report.contains("<th>Losses</th><td>1</td>"));
    assert!(report.contains("<th>Net PnL</th><td>-2.000000000000000000</td>"));
}

#[test]
fn publishes_undefined_profit_factor_when_no_trade_loses() {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let minute = 60_000_000_000;
    let quotes = [quote(start, "9", "10"), quote(start + minute, "12", "13")];
    let (metrics, report) = run_job("win-only", &quotes, &[("buy", 1), ("sell", 1)]);
    let trades = &metrics["trades"];
    assert_eq!(trades["trade_count"], 1);
    assert_eq!(trades["win_count"], 1);
    assert_eq!(trades["loss_count"], 0);
    assert_eq!(trades["hit_rate"], "1.000000000000000000");
    assert_eq!(trades["net_pnl"], "2.000000000000000000");
    assert_eq!(trades["gross_profit"], "2.000000000000000000");
    assert_eq!(
        trades["gross_loss"], "0E-18",
        "scale-18 zero renders like Python str(Decimal('0E-18'))"
    );
    assert!(trades["profit_factor"].is_null(), "no loss is undefined");
    assert_eq!(trades["profit_factor_reason"], "no_net_losses");
    assert!(trades["average_net_loss"].is_null());
    assert!(trades["largest_net_loss"].is_null());
    assert!(report.contains("<th>Trades</th><td>1</td>"));
    assert!(report.contains("<th>Net PnL</th><td>2.000000000000000000</td>"));
}

#[test]
fn position_flip_closes_the_long_then_realizes_the_short() {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let minute = 60_000_000_000;
    // Buy 2 at 10; sell 3 at 12 closes both (trade +4) and opens a short at 12;
    // buy 1 at 14 closes the short (trade -2).
    let quotes = [
        quote(start, "9", "10"),
        quote(start + minute, "12", "13"),
        quote(start + 2 * minute, "13", "14"),
    ];
    let (metrics, _) = run_job("flip", &quotes, &[("buy", 2), ("sell", 3), ("buy", 1)]);
    let trades = &metrics["trades"];
    assert_eq!(trades["trade_count"], 2);
    assert_eq!(trades["win_count"], 1);
    assert_eq!(trades["loss_count"], 1);
    assert_eq!(trades["gross_profit"], "4.000000000000000000");
    assert_eq!(trades["gross_loss"], "2.000000000000000000");
    assert_eq!(trades["profit_factor"], "2.000000000000000000");
    assert_eq!(trades["net_pnl"], "2.000000000000000000");
}

#[test]
fn fifo_closes_the_oldest_lot_first() {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let minute = 60_000_000_000;
    // Long 10 then long 14; sell at 12 closes the oldest lot (+2) and sell at
    // 13 closes the newest (-1). LIFO matching would publish different sums.
    let quotes = [
        quote(start, "9", "10"),
        quote(start + minute, "13", "14"),
        quote(start + 2 * minute, "12", "13"),
        quote(start + 3 * minute, "13", "14"),
    ];
    let (metrics, _) = run_job(
        "fifo",
        &quotes,
        &[("buy", 1), ("buy", 1), ("sell", 1), ("sell", 1)],
    );
    let trades = &metrics["trades"];
    assert_eq!(trades["trade_count"], 2);
    assert_eq!(trades["win_count"], 1);
    assert_eq!(trades["loss_count"], 1);
    assert_eq!(trades["gross_profit"], "2.000000000000000000");
    assert_eq!(trades["gross_loss"], "1.000000000000000000");
    assert_eq!(trades["profit_factor"], "2.000000000000000000");
    assert_eq!(trades["net_pnl"], "1.000000000000000000");
}

#[test]
fn publishes_an_empty_trades_block_when_no_order_fills() {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let minute = 60_000_000_000;
    let (metrics, report) = run_job(
        "no-trades",
        &[quote(start, "9", "10"), quote(start + minute, "10", "11")],
        &[],
    );
    let trades = &metrics["trades"];
    assert_eq!(trades["trade_count"], 0);
    assert_eq!(trades["win_count"], 0);
    assert_eq!(trades["loss_count"], 0);
    assert!(trades["hit_rate"].is_null());
    assert_eq!(trades["hit_rate_reason"], "no_closed_trades");
    assert!(trades["profit_factor"].is_null());
    assert_eq!(trades["profit_factor_reason"], "no_closed_trades");
    assert!(trades["average_net_win"].is_null());
    assert!(trades["average_net_loss"].is_null());
    assert!(trades["largest_net_win"].is_null());
    assert!(trades["largest_net_loss"].is_null());
    assert!(report.contains("<th>Trades</th><td>0</td>"));
    assert!(metrics["equity"]["sharpe"].is_null());
    assert_eq!(
        metrics["equity"]["sharpe_reason"],
        "fewer_than_three_samples"
    );
}

#[test]
fn allocates_fees_by_quantity_share_and_balances_the_ledger() {
    let start = parse_timestamp_ns("2024-01-02T14:30:00Z").expect("start");
    let minute = 60_000_000_000;
    // Buy 4 at 10 and buy 6 at 11 (0.10 per share: fees 0.40 and 0.60). Sell 10
    // at 12 (fee 1.00) closes both lots, so the sell fee splits 0.40 / 0.60 by
    // quantity and each slice carries its opening fee.
    let quotes = [
        quote(start, "9", "10"),
        quote(start + minute, "10", "11"),
        quote(start + 2 * minute, "12", "13"),
    ];
    let mut configured = config();
    configured.fees.rules.push(FeeRule {
        fee_id: "commission".to_string(),
        basis: FeeBasis::PerUnit,
        rate: "0.10".parse().expect("rate"),
        minimum: num::Decimal::from_i128(0),
        side: "both".to_string(),
    });
    let (metrics, _) = run_job_with(
        "fee-allocation",
        configured,
        &quotes,
        &[("buy", 4), ("buy", 6), ("sell", 10)],
    );
    let trades = &metrics["trades"];
    assert_eq!(trades["schema"], "closed_trades.v1");
    assert_eq!(trades["fee_allocation"], "fill_quantity_share.v1");
    let rows = trades["rows"].as_array().expect("trade rows");
    assert_eq!(rows.len(), 2);
    let first_entry = rows[0]["entry_sequence"].as_u64().expect("entry sequence");
    let second_entry = rows[1]["entry_sequence"].as_u64().expect("entry sequence");
    let exit = rows[0]["exit_sequence"].as_u64().expect("exit sequence");
    assert!(
        first_entry < second_entry && second_entry < exit,
        "FIFO opens precede the closing sell"
    );
    assert_eq!(rows[1]["exit_sequence"], rows[0]["exit_sequence"]);
    assert_eq!(rows[0]["quantity"], "4.000000000000000000");
    assert_eq!(rows[0]["gross_pnl"], "8.000000000000000000");
    assert_eq!(rows[0]["entry_fee"], "0.400000000000000000");
    assert_eq!(rows[0]["exit_fee"], "0.400000000000000000");
    assert_eq!(rows[0]["net_pnl"], "7.200000000000000000");
    assert_eq!(rows[1]["quantity"], "6.000000000000000000");
    assert_eq!(rows[1]["gross_pnl"], "6.000000000000000000");
    assert_eq!(rows[1]["entry_fee"], "0.600000000000000000");
    assert_eq!(rows[1]["exit_fee"], "0.600000000000000000");
    assert_eq!(rows[1]["net_pnl"], "4.800000000000000000");
    assert_eq!(trades["gross_pnl"], "14.000000000000000000");
    assert_eq!(trades["fees"], "2.000000000000000000");
    assert_eq!(trades["net_pnl"], "12.000000000000000000");
    assert_eq!(metrics["execution"]["fees_total"], "2.000000000000000000");
    let entry_time = rows[0]["entry_timestamp_ns"].as_i64().expect("entry time");
    let exit_time = rows[0]["exit_timestamp_ns"].as_i64().expect("exit time");
    assert!(entry_time < exit_time, "a slice opens before it closes");
    assert!(
        metrics["equity"]["sharpe"].is_string(),
        "four samples define a Sharpe"
    );
    assert!(metrics["equity"]["sharpe_reason"].is_null());
    let open_fees: f64 = trades["open_fees"]
        .as_str()
        .expect("open fees")
        .parse()
        .expect("decimal");
    assert!(open_fees.abs() < f64::EPSILON, "every lot closed");
}
