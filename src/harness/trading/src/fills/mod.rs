//! Deterministic fill and fee models.
//!
//! Ports `harness.fills`: the four fill-decision plugins plus the
//! configuration-driven fee models and aggregation.

pub mod decision;
pub mod fees;
pub mod latency_size;
pub mod mid_touch;
pub mod simple_touch;
pub mod trade_through;

pub use decision::FillDecision;
pub use fees::{
    aggregate_fees, AssetClass, CryptoMakerTakerFeeModel, EquityRegulatoryFeeModel, FeeAggregate,
    FeeAggregation, FeeAssessment, FeeComponent, FeeContext, FeeRounding, FeeRoundingMode,
    FeeSchedule, LiquidityRole, OptionsPerContractFeeModel,
};
pub use latency_size::{LatencySize, RemainderPolicy};
pub use mid_touch::MidTouch;
pub use simple_touch::SimpleTouch;
pub use trade_through::TradeThrough;

pub mod trades;
