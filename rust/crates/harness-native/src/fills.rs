//! Deterministic fill models for single-leg orders, ported from
//! `src/harness/fills`.
//!
//! This is the stable facade of the fills package: [`crate::simple_touch`]
//! (`SimpleTouch` + the shared `FillDecision`), [`crate::latency_size`]
//! (`LatencySize`), and [`crate::trade_through`] (`TradeThrough`) mirror their
//! Zig counterparts. Every decision is an exact `i128` fixed-point value at
//! scale 18.

use crate::market::{Error, MarketEvent, MarketOrder};
use crate::simple_touch::{DecideOptions, FillDecision, SimpleTouch};

pub use crate::latency_size::{LatencyMetadata, LatencySize, LatencySizeConfig};
pub use crate::simple_touch::{
    displayed_quantity, is_triggered, resolve_remaining, validate_pair, Liquidity, RemainderPolicy,
};
pub use crate::trade_through::{TradeThrough, TradeThroughConfig, TradeThroughMetadata};

/// The fill plugin interface consumed by the simulated venue.
#[derive(Clone, Debug, Default)]
pub enum FillModel {
    /// Deterministic top-of-book touch (the venue default).
    #[default]
    SimpleTouch,
    /// Displayed-size constrained latency model.
    LatencySize(LatencySize),
    /// Strict trade-print model.
    TradeThrough(TradeThrough),
}

impl FillModel {
    /// Decide a fill for `order` against `snap`.
    pub fn decide<'a>(
        &mut self,
        order: &MarketOrder<'a>,
        snap: &MarketEvent<'a>,
        opts: &DecideOptions,
    ) -> Result<Option<FillDecision<'a>>, Error> {
        match self {
            Self::SimpleTouch => SimpleTouch.decide(order, snap, opts),
            Self::LatencySize(model) => model.decide(order, snap, opts),
            Self::TradeThrough(model) => model.decide(order, snap, opts),
        }
    }

    /// True when this model is the stateless simple-touch model.
    pub fn is_simple_touch(&self) -> bool {
        matches!(self, Self::SimpleTouch)
    }
}

#[cfg(test)]
mod tests {
    use super::{FillModel, LatencySize, LatencySizeConfig};
    use crate::market::{OrderType, Side};
    use crate::simple_touch::testing::{
        default_quote, expect_decision, make_order, ONE0ONE, TEN,
    };
    use crate::simple_touch::{DecideOptions, Liquidity};

    #[test]
    fn fill_model_interface_dispatches_by_plugin_kind() {
        let buy = make_order("interface", Side::Buy, OrderType::Market, TEN, None, None);
        let mut touch = FillModel::SimpleTouch;
        let via_touch = touch
            .decide(&buy, &default_quote("q"), &DecideOptions::default())
            .unwrap();
        expect_decision(via_touch, TEN, ONE0ONE, Liquidity::Taker);
        assert!(touch.is_simple_touch());

        let mut latency = FillModel::LatencySize(LatencySize::new(LatencySizeConfig::default()));
        let via_latency = latency
            .decide(&buy, &default_quote("q"), &DecideOptions::default())
            .unwrap();
        expect_decision(via_latency, TEN, ONE0ONE, Liquidity::Taker);
        assert!(!latency.is_simple_touch());
    }
}
