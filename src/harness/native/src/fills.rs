//! Deterministic fill models for single-leg orders, ported from
//! `src/harness/fills`.
//!
//! This is the stable facade of the fills package: [`crate::simple_touch`]
//! (`SimpleTouch` + the shared `FillDecision`), [`crate::latency_size`]
//! (`LatencySize`), and [`crate::trade_through`] (`TradeThrough`). Every
//! decision is an exact `i128` fixed-point value at scale 18.

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
