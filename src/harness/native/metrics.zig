//! Metrics facade. The port lives in the ``metrics/`` folder, split by
//! concern with tests colocated next to each module:
//! ``common`` (ledger types and pinned-scale conventions), ``equity``
//! (total return, CAGR, drawdowns), ``risk`` (Sharpe, Sortino, the
//! scale-18 square root), ``trades`` (realized-trade aggregates),
//! ``execution`` (slippage, fees, latency, order outcomes), and
//! ``attribution`` (per-strategy grouping). Each submodule mirrors the
//! conventions of ``src/harness/results/metrics.py`` on the pinned
//! scale-18 fixed-point kernel.

const common = @import("metrics/common.zig");
const equity = @import("metrics/equity.zig");
const risk = @import("metrics/risk.zig");
const trades = @import("metrics/trades.zig");
const execution = @import("metrics/execution.zig");
const attribution = @import("metrics/attribution.zig");

pub const decimal = common.decimal;
pub const MetricsError = common.MetricsError;
pub const AttributionError = common.AttributionError;
pub const EquityPoint = common.EquityPoint;
pub const TradeRow = common.TradeRow;
pub const FillRow = common.FillRow;
pub const OrderRow = common.OrderRow;
pub const DrawdownSpan = common.DrawdownSpan;
pub const EquityMetrics = common.EquityMetrics;
pub const TradeStats = common.TradeStats;
pub const LatencyStats = common.LatencyStats;
pub const OrderOutcomeCounts = common.OrderOutcomeCounts;
pub const ExecutionStats = common.ExecutionStats;
pub const StrategyAttribution = common.StrategyAttribution;
pub const AnnotatedFill = common.AnnotatedFill;
pub const AnnotatedOrder = common.AnnotatedOrder;
pub const AnnotatedTrade = common.AnnotatedTrade;

pub const decSqrt = risk.decSqrt;
pub const sharpe = risk.sharpe;
pub const sortino = risk.sortino;

pub const totalReturn = equity.totalReturn;
pub const cagr = equity.cagr;
pub const maxDrawdown = equity.maxDrawdown;
pub const longestDrawdown = equity.longestDrawdown;
pub const equityCurveMetrics = equity.equityCurveMetrics;

pub const tradeStats = trades.tradeStats;

pub const fillSlippage = execution.fillSlippage;
pub const latencyStats = execution.latencyStats;
pub const orderOutcomeCounts = execution.orderOutcomeCounts;
pub const executionStats = execution.executionStats;

pub const perStrategyAttribution = attribution.perStrategyAttribution;

test {
    _ = common;
    _ = equity;
    _ = risk;
    _ = trades;
    _ = execution;
    _ = attribution;
}
