//! Offline static HTML report for one recorded run.
//!
//! Deterministic core of `harness.results.report`. The renderer is a pure
//! mapping from artifact data to a self-contained HTML string: inline CSS and a
//! hand-rendered inline SVG chart, with no JavaScript, web fonts, images, or any
//! external asset. It carries no wall-clock timestamp, so the same data always
//! produces byte-identical HTML.
//!
//! The full Python renderer also draws downsampled position, risk, and slippage
//! sections; the staged Rust port renders the manifest, equity metrics, and the
//! equity curve. The remaining sections land with the results-store port.

use std::fmt::Write as _;

use crate::results::metrics::{equity_curve_metrics, EquityPoint, TradeStats};

const CSS: &str = ":root{color-scheme:light}body{margin:0;padding:2rem;background:#f8fafc;\
color:#1f2937;font-family:system-ui,\"Segoe UI\",sans-serif}main{max-width:1080px;margin:0 auto}\
h1{font-size:1.35rem}h2{font-size:1.05rem;color:#0f172a}section{background:#fff;\
border:1px solid #e5e7eb;border-radius:8px;padding:1.25rem;margin:0 0 1.25rem}\
table{border-collapse:collapse;width:100%;font-size:.85rem}th,td{text-align:left;\
padding:.3rem .5rem;border-bottom:1px solid #e5e7eb}th{color:#64748b}\
code{font-family:ui-monospace,Consolas,monospace}svg{width:100%;height:auto}\
.line{fill:none;stroke:#2563eb;stroke-width:2}";

/// Manifest header facts rendered at the top of every report.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReportHeader {
    /// Run identifier.
    pub run_id: String,
    /// Engine identifier.
    pub engine: String,
    /// Recorded seed.
    pub seed: String,
    /// Free-form metadata, rendered in sorted key order.
    pub metadata: std::collections::BTreeMap<String, String>,
}

impl ReportHeader {
    /// Build a header for one run.
    #[must_use]
    pub fn new(run_id: impl Into<String>) -> Self {
        Self {
            run_id: run_id.into(),
            engine: "harness.backtest".to_string(),
            seed: String::new(),
            metadata: std::collections::BTreeMap::new(),
        }
    }
}

fn escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

fn sample_indices(count: usize, limit: usize) -> Vec<usize> {
    if count <= limit || limit < 2 {
        return (0..count).collect();
    }
    let mut picked: Vec<usize> = (0..limit - 1)
        .map(|index| index * count / (limit - 1))
        .collect();
    picked.push(count - 1);
    picked.sort_unstable();
    picked.dedup();
    picked
}

fn equity_svg(points: &[EquityPoint]) -> String {
    let picked = sample_indices(points.len(), 256);
    if picked.is_empty() {
        return String::new();
    }
    let values: Vec<f64> = picked
        .iter()
        .map(|index| points[*index].equity.to_f64())
        .collect();
    let timestamps: Vec<i64> = picked
        .iter()
        .map(|index| points[*index].timestamp_ns)
        .collect();
    let low = values.iter().copied().fold(f64::INFINITY, f64::min);
    let high = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let span = if (high - low).abs() < f64::EPSILON {
        1.0
    } else {
        high - low
    };
    let t0 = timestamps[0];
    let t1 = *timestamps.last().unwrap_or(&t0);
    let width = 960.0f64;
    let height = 180.0f64;
    let coords: Vec<String> = picked
        .iter()
        .enumerate()
        .map(|(position, _)| {
            let x = if t1 == t0 {
                width / 2.0
            } else {
                (timestamps[position] - t0) as f64 / (t1 - t0) as f64 * width
            };
            let y = height - (values[position] - low) / span * height;
            format!("{x:.2},{y:.2}")
        })
        .collect();
    let mut svg = String::new();
    let _ = write!(
        svg,
        "<svg class=\"chart\" viewBox=\"0 0 960 180\" role=\"img\" \
aria-label=\"Equity curve\"><polyline class=\"line\" points=\"{}\" /></svg>",
        coords.join(" ")
    );
    svg
}

/// Render one run's data as a self-contained HTML document.
///
/// The document is a pure function of `header`, `equity`, and `trades`: no wall
/// clock, randomness, I/O, or network access participates, and repeated calls
/// return byte-identical strings.
#[must_use]
pub fn render_report(header: &ReportHeader, equity: &[EquityPoint], trades: &TradeStats) -> String {
    let metrics = equity_curve_metrics(equity);
    let mut document = String::new();
    document.push_str("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">");
    let _ = write!(
        document,
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\
<title>FCA Backtest Report - {}</title><style>{CSS}</style></head><body><main>",
        escape(&header.run_id)
    );
    let _ = write!(
        document,
        "<h1>FCA Backtest Report <code>{}</code></h1>",
        escape(&header.run_id)
    );
    document.push_str("<section><h2>Manifest</h2><table>");
    let _ = write!(
        document,
        "<tr><th>Run ID</th><td>{}</td></tr>",
        escape(&header.run_id)
    );
    let _ = write!(
        document,
        "<tr><th>Engine</th><td>{}</td></tr>",
        escape(&header.engine)
    );
    let _ = write!(
        document,
        "<tr><th>Seed</th><td>{}</td></tr>",
        escape(&header.seed)
    );
    let _ = write!(
        document,
        "<tr><th>Equity samples</th><td>{}</td></tr>",
        equity.len()
    );
    for (key, value) in &header.metadata {
        let _ = write!(
            document,
            "<tr><th>{}</th><td>{}</td></tr>",
            escape(key),
            escape(value)
        );
    }
    document.push_str("</table></section>");

    document.push_str("<section><h2>Equity and drawdown</h2>");
    let total_return = metrics
        .total_return
        .map_or_else(|| "-".to_string(), |value| value.to_string());
    let cagr = metrics
        .cagr
        .map_or_else(|| "-".to_string(), |value| format!("{value:.4}"));
    let _ = write!(
        document,
        "<p><code>Total return {total_return}</code> <code>CAGR {cagr}</code> \
<code>Max drawdown {}</code></p>",
        metrics.max_drawdown
    );
    document.push_str(&equity_svg(equity));
    document.push_str("</section>");

    document.push_str("<section><h2>Trades</h2><table>");
    let _ = write!(
        document,
        "<tr><th>Trades</th><td>{}</td></tr>",
        trades.trade_count
    );
    let _ = write!(
        document,
        "<tr><th>Wins</th><td>{}</td></tr>",
        trades.win_count
    );
    let _ = write!(
        document,
        "<tr><th>Losses</th><td>{}</td></tr>",
        trades.loss_count
    );
    let _ = write!(
        document,
        "<tr><th>Total PnL</th><td>{}</td></tr>",
        trades.total_pnl
    );
    document.push_str("</table></section>");
    document.push_str("<footer>Self-contained offline report: inline CSS and SVG only.</footer>");
    document.push_str("</main></body></html>");
    document
}

/// Return [`render_report`] as deterministic UTF-8 bytes.
#[must_use]
pub fn render_report_bytes(
    header: &ReportHeader,
    equity: &[EquityPoint],
    trades: &TradeStats,
) -> Vec<u8> {
    render_report(header, equity, trades).into_bytes()
}

#[cfg(test)]
mod tests {
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
}
