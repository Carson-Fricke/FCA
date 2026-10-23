//! Deterministic pre-trade risk engine, rules, limits, and collateral.
//!
//! Ports `harness.risk`: the rule/pipeline model, exact-decimal limits, the
//! audited global kill switch, and option collateral rules.

pub mod collateral;
pub mod kill_switch;
pub mod limits;
pub mod models;
pub mod pipeline;

pub use collateral::{
    option_collateral_report, ContractCollateral, NoNakedShortOptions, OptionCollateralLimit,
    OptionCollateralReport, OptionGroupCollateral,
};
pub use kill_switch::{
    KillSwitch, KillSwitchAction, KillSwitchEvent, KillSwitchState, DEFAULT_KILL_SWITCH_RULE_ID,
};
pub use limits::{
    order_notional, order_price, order_size, scale_order, DailyLossHalt, MaxGrossExposure,
    MaxNetExposure, MaxOrderCount, MaxOrderNotional, MaxOrderSize, MaxPositionNotional,
};
pub use models::{RiskAction, RiskAudit, RiskContext, RiskDecision, RiskRule, RuleDecision};
pub use pipeline::{OnReject, RiskPipeline};
