use super::*;
use crate::results::metrics::{equity_curve_metrics, TradeRow};

fn point(timestamp_ns: i64, equity: &str) -> EquityPoint {
    EquityPoint {
        timestamp_ns,
        equity: equity.parse().expect("equity"),
    }
}

#[test]
fn report_is_deterministic_and_self_contained() {
    let header = ReportHeader::new("bt-1");
    let equity = [point(0, "100"), point(10, "110"), point(20, "105")];
    let trades = crate::results::metrics::trade_stats(&[TradeRow {
        trade_id: "t".to_string(),
        strategy_id: "s".to_string(),
        realized_pnl: "5".parse().expect("pnl"),
    }]);
    let first = render_report(&header, &equity, &trades);
    let second = render_report(&header, &equity, &trades);
    assert_eq!(first, second);
    assert!(first.starts_with("<!doctype html>"));
    assert!(first.contains("bt-1"));
    assert!(first.contains("<polyline"));
    assert!(!first.contains("http://"));
    assert!(!first.contains("https://"));
    assert!(!equity_curve_metrics(&equity)
        .max_drawdown
        .to_string()
        .is_empty());
}

#[test]
fn escapes_untrusted_text() {
    let mut header = ReportHeader::new("<script>");
    header.metadata.insert("k<".to_string(), "v&".to_string());
    let report = render_report(&header, &[], &crate::results::metrics::trade_stats(&[]));
    assert!(!report.contains("<script>"));
    assert!(report.contains("&lt;script&gt;"));
    assert!(report.contains("k&lt;"));
}
