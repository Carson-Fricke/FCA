//! Projects validated replay rules into shared trading risk policy; no execution or persistence.
use crate::backtest::config::BacktestConfig;
use harness_contracts::canonical_json::JsonValue;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use num::Decimal;
use std::collections::BTreeMap;

/// One risk audit entry frozen for the artifact.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskAudit {
    /// Audit sequence.
    pub sequence: u64,
    /// Rule pass number.
    pub pass_number: u64,
    /// Rule identifier.
    pub rule_id: String,
    /// `allow`, `reduce`, or `reject`.
    pub action: String,
    /// Audit reason.
    pub reason: Option<String>,
    /// Whether the rule requested a halt.
    pub halt: bool,
}

/// One complete recorded risk decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskDecision {
    /// Decision action.
    pub action: String,
    /// Original order identifier.
    pub order_id: String,
    /// Strategy instance identifier.
    pub strategy_id: String,
    /// Decision reason.
    pub reason: Option<String>,
    /// Halt scope.
    pub halt_scope: Option<String>,
    /// Ordered audits.
    pub audits: Vec<RiskAudit>,
}

/// One configured risk rule resolved to a cap.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedRiskRule {
    /// Rule identifier.
    pub rule_id: String,
    /// Rule kind.
    pub kind: RiskRuleKind,
    /// Numeric limit.
    pub limit: Decimal,
}

/// Supported built-in risk rule kinds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RiskRuleKind {
    /// Cap the gross notional of one candidate order (`MaxOrderNotional`).
    MaxOrderNotional,
    /// Cap the resulting notional of each changed position (`MaxPositionNotional`).
    MaxPositionNotional,
}

/// Resolve the configured risk rules into evaluated built-ins.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] on an unsupported implementation or
/// missing limit.
pub fn resolve_risk_rules(
    configuration: &BacktestConfig,
) -> Result<Vec<ResolvedRiskRule>, BacktestError> {
    let mut rules = Vec::new();
    for rule in &configuration.risk_pipeline.rules {
        let class = rule
            .implementation
            .rsplit(':')
            .next()
            .unwrap_or(rule.implementation.as_str());
        let kind = match class {
            "MaxOrderNotional" => RiskRuleKind::MaxOrderNotional,
            "MaxPositionNotional" => RiskRuleKind::MaxPositionNotional,
            other => {
                return Err(BacktestError::Configuration(format!(
                    "unsupported risk rule implementation {other:?}"
                )));
            }
        };
        let limit = rule
            .parameters
            .get("limit")
            .and_then(parameter_decimal)
            .ok_or_else(|| {
                BacktestError::Configuration(format!(
                    "risk rule {:?} requires a 'limit' parameter",
                    rule.rule_id
                ))
            })?;
        rules.push(ResolvedRiskRule {
            rule_id: rule.rule_id.clone(),
            kind,
            limit,
        });
    }
    Ok(rules)
}

fn parameter_decimal(value: &JsonValue) -> Option<Decimal> {
    match value {
        JsonValue::String(text) => text.parse().ok(),
        JsonValue::Object(entries) => entries.get("$decimal").and_then(|inner| match inner {
            JsonValue::String(text) => text.parse().ok(),
            _ => None,
        }),
        JsonValue::Int(value) => Some(Decimal::from_i128(i128::from(*value))),
        _ => None,
    }
}

/// Evaluate one configured rule against a candidate order.
///
/// # Errors
///
/// Returns [`BacktestError::Arithmetic`] when the notional cannot be represented.
/// An unrepresentable notional is never treated as zero, which would pass any
/// limit.
pub(super) fn evaluate_rule(
    rule: &ResolvedRiskRule,
    order: &EngineOrder,
    prices: &BTreeMap<String, Decimal>,
    current_quantity: Decimal,
) -> Result<(String, Option<String>, Option<Decimal>), BacktestError> {
    use harness_trading::notional_policy::{Cap, Decision};
    let cap = match rule.kind {
        RiskRuleKind::MaxOrderNotional => Cap::Order,
        RiskRuleKind::MaxPositionNotional => Cap::Position,
    };
    let Some(price) = order_price(order, prices) else {
        return Ok((
            "reject".into(),
            Some(format!("{} missing price", cap.name())),
            None,
        ));
    };
    let side = if order.side == "buy" {
        harness_trading::orders::Side::Buy
    } else {
        harness_trading::orders::Side::Sell
    };
    let decision = harness_trading::notional_policy::evaluate(
        cap,
        order.quantity,
        side,
        price,
        current_quantity,
        rule.limit,
    )
    .map_err(|e| {
        BacktestError::Arithmetic(format!(
            "{} of order {} cannot be represented: {e}",
            cap.name(),
            order.order_id
        ))
    })?;
    Ok(match decision {
        Decision::Allow => ("allow".into(), None, None),
        Decision::Reduce(quantity) => (
            "reduce".into(),
            Some(format!("{} reduced order", cap.name())),
            Some(quantity),
        ),
        Decision::Reject => (
            "reject".into(),
            Some(format!("{} has no permitted quantity", cap.name())),
            None,
        ),
    })
}

pub(super) fn order_price(
    order: &EngineOrder,
    prices: &BTreeMap<String, Decimal>,
) -> Option<Decimal> {
    if let Some(limit) = order.limit_price {
        return Some(limit);
    }
    if let Some(stop) = order.stop_price {
        return Some(stop);
    }
    prices.get(&order.symbol).copied()
}
