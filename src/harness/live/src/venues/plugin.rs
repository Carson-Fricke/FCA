//! Pluggable fill-decision models behind a single object-safe trait.

use num::Decimal18;

use harness_trading::error::LiveResult;
use harness_trading::events::MarketEvent;
use harness_trading::fills::decision::FillDecision;
use harness_trading::fills::latency_size::LatencySize;
use harness_trading::fills::mid_touch::MidTouch;
use harness_trading::fills::simple_touch::SimpleTouch;
use harness_trading::fills::trade_through::TradeThrough;
use harness_trading::orders::SingleLegOrder;

/// A deterministic fill plugin used by the simulated venue.
pub trait FillPlugin {
    /// Return a fill decision, or `None` when the order does not execute.
    fn decide(
        &mut self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining: Option<Decimal18>,
        available: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>>;

    /// Return whether a stop order is armed by this market snapshot.
    fn is_triggered(&self, order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<bool>;
}

impl FillPlugin for SimpleTouch {
    fn decide(
        &mut self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining: Option<Decimal18>,
        available: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>> {
        SimpleTouch::decide(self, order, market, remaining, available, stop_triggered)
    }

    fn is_triggered(&self, order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<bool> {
        SimpleTouch::is_triggered(self, order, market)
    }
}

impl FillPlugin for MidTouch {
    fn decide(
        &mut self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining: Option<Decimal18>,
        available: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>> {
        MidTouch::decide(self, order, market, remaining, available, stop_triggered)
    }

    fn is_triggered(&self, order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<bool> {
        MidTouch::is_triggered(self, order, market)
    }
}

impl FillPlugin for TradeThrough {
    fn decide(
        &mut self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining: Option<Decimal18>,
        available: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>> {
        TradeThrough::decide(self, order, market, remaining, available, stop_triggered)
    }

    fn is_triggered(&self, order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<bool> {
        SimpleTouch::new().is_triggered(order, market)
    }
}

impl FillPlugin for LatencySize {
    fn decide(
        &mut self,
        order: &SingleLegOrder,
        market: &MarketEvent,
        remaining: Option<Decimal18>,
        available: Option<Decimal18>,
        stop_triggered: bool,
    ) -> LiveResult<Option<FillDecision>> {
        LatencySize::decide(self, order, market, remaining, available, stop_triggered)
    }

    fn is_triggered(&self, order: &SingleLegOrder, market: &MarketEvent) -> LiveResult<bool> {
        SimpleTouch::new().is_triggered(order, market)
    }
}
