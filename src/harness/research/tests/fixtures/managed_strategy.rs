//! Strategy policy only: the harness owns identities, pending orders and money.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_strategy_program, Decimal, MarketConsumer, MetricPublisher, Order, OrderSink,
    QuoteEvent, Side, StrategyProgram, Subscription,
};

struct Managed {
    closing: bool,
    direction: i128,
    order_types: bool,
    cancel_order: bool,
    sent: bool,
}
impl MarketConsumer for Managed {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: vec!["SPY".into()],
        }]
    }
    fn on_quote(&mut self, _: &QuoteEvent, ctx: &mut OrderSink) {
        if self.cancel_order {
            if !self.sent {
                let id = ctx
                    .place(Order::limit("SPY", Side::Buy, Decimal::ONE, Decimal::ONE))
                    .expect("limit order");
                ctx.cancel(&id).expect("cancel owned order");
                self.sent = true;
            }
            return;
        }
        if self.order_types {
            if !self.sent {
                for order in [
                    Order::market("SPY", Side::Buy, Decimal::ONE),
                    Order::limit("SPY", Side::Buy, Decimal::ONE, Decimal::from_i128(101)),
                    Order::stop("SPY", Side::Buy, Decimal::ONE, Decimal::from_i128(99)),
                    Order::stop_limit(
                        "SPY",
                        Side::Buy,
                        Decimal::ONE,
                        Decimal::from_i128(99),
                        Decimal::from_i128(101),
                    ),
                ] {
                    ctx.place(order).expect("valid order request");
                }
                self.sent = true;
            }
            return;
        }
        if ctx.execution().quantity("SPY") == Decimal::from_i128(10 * self.direction) {
            self.closing = true;
        }
        if self.closing {
            ctx.close_position("SPY").expect("close target");
        } else {
            ctx.target_position("SPY", Decimal::from_i128(10 * self.direction))
                .expect("entry target");
        }
    }
}
impl StrategyProgram for Managed {
    fn new(parameters: Value) -> Self {
        Self {
            closing: false,
            direction: parameters["direction"].as_i64().unwrap_or(1) as i128,
            order_types: parameters["order_types"].as_bool().unwrap_or(false),
            cancel_order: parameters["cancel_order"].as_bool().unwrap_or(false),
            sent: false,
        }
    }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let state = metrics.execution().expect("strategy account");
        let realized = state.realized_pnl.to_f64_checked().expect("finite PnL");
        let trades = metrics.closed_trades();
        let mean_return = if trades.is_empty() {
            0.0
        } else {
            trades
                .iter()
                .map(|t| t.net_return_bps.expect("positive notional").to_f64())
                .sum::<f64>()
                / trades.len() as f64
        };
        let order_count = metrics.orders().len() as f64;
        metrics
            .publish("sdk_order_count", order_count)
            .expect("metric");
        metrics
            .publish("sdk_realized_pnl", realized)
            .expect("metric");
        metrics
            .publish("sdk_mean_trade_return_bps", mean_return)
            .expect("metric");
    }
}
export_strategy_program!(Managed);
