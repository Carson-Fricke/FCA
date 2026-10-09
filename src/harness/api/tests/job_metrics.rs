//! Job receipts project canonical financial quantities and undefined reasons.
use api::http::launcher::artifact_metrics;
use std::collections::BTreeMap;

#[test]
fn job_metrics_preserve_decimal_spelling_and_undefined_reasons() {
    let artifact = serde_json::json!({
        "equity":{"net_pnl":"10", "gross_pnl":"12", "total_return":"0.010000000000000000", "sharpe_per_sample":null,"sharpe_per_sample_reason":"insufficient_samples"},
        "trades":{"gross_pnl":"12", "net_pnl":"10", "fees":"2", "open_fees":"0.5", "profit_factor":null,"profit_factor_reason":"no_losses"},
        "custom":{"net_pnl":"999", "signal_score":"0.4"}
    });
    let metrics = artifact_metrics(&BTreeMap::from([(
        "metrics.json".to_string(),
        serde_json::to_vec(&artifact).unwrap(),
    )]));
    assert_eq!(metrics["total_return"], "0.010000000000000000");
    assert_eq!(metrics["net_pnl"], "10");
    assert_eq!(metrics["gross_pnl"], "12");
    assert_eq!(metrics["closed_trade_net_pnl"], "10");
    assert_eq!(metrics["open_fees"], "0.5");
    assert_eq!(metrics["profit_factor_reason"], "no_losses");
    assert_eq!(metrics["sharpe_per_sample_reason"], "insufficient_samples");
    assert!(!metrics.contains_key("profit_factor"));
    assert_eq!(metrics["signal_score"], "0.4");
}
