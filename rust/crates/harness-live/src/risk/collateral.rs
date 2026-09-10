//! Exact-decimal option collateral rules as pre-trade risk rules.
//!
//! Collateral is derived from the candidate order plus the point-in-time
//! portfolio snapshot only: contract terms come from canonical OSI symbols and
//! no mark price, clock, catalog, or randomness is ever consulted. Each
//! `(root, expiration)` contract group is evaluated exactly at its strike
//! breakpoints.

use std::collections::{BTreeSet, HashMap};

use harness_num::Decimal18;

use crate::error::{require_text, LiveError, LiveResult};
use crate::orders::{OrderRequest, Side};
use crate::risk::models::{RiskContext, RiskRule, RuleDecision};
use crate::symbol::{OsiSymbol, Symbol};

const ZERO: Decimal18 = Decimal18::ZERO;
const DEFAULT_MULTIPLIER: i64 = 100;

/// Per-contract projected collateral classification.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContractCollateral {
    /// The contract symbol.
    pub symbol: String,
    /// The underlying root.
    pub root: String,
    /// The expiration as `YYMMDD`.
    pub expiration: String,
    /// `C` or `P`.
    pub right: char,
    /// Signed position after the candidate order.
    pub projected_contracts: Decimal18,
    /// Contract quantity covered by underlying shares.
    pub covered_by_shares: Decimal18,
    /// Whether same-root, same-expiry, same-right longs make a spread.
    pub covered_by_long_option: bool,
    /// Short quantity with no covering instrument.
    pub naked_contracts: Decimal18,
}

/// Exact collateral requirement for one root and expiration group.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionGroupCollateral {
    /// The underlying root.
    pub root: String,
    /// The expiration as `YYMMDD`.
    pub expiration: String,
    /// Per-contract classifications.
    pub contracts: Vec<ContractCollateral>,
    /// Worst-case additional cash, or `None` when unlimited.
    pub requirement: Option<Decimal18>,
    /// Whether the group's worst case is unlimited.
    pub unlimited: bool,
    /// Short puts with no covering instrument.
    pub naked_put_contracts: Decimal18,
    /// Short calls with no covering instrument.
    pub naked_call_contracts: Decimal18,
}

/// Complete per-contract collateral classification for one evaluation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionCollateralReport {
    /// Contracts-to-shares multiplier.
    pub multiplier: Decimal18,
    /// Contract groups.
    pub groups: Vec<OptionGroupCollateral>,
}

impl OptionCollateralReport {
    /// Summed group requirement, or `None` if unlimited.
    pub fn total_collateral(&self) -> Option<Decimal18> {
        let mut total = ZERO;
        for group in &self.groups {
            total += group.requirement?;
        }
        Some(total)
    }

    /// Whether any group has an unlimited worst case.
    pub fn unlimited(&self) -> bool {
        self.groups.iter().any(|group| group.unlimited)
    }

    /// Total short puts with no covering instrument.
    pub fn naked_put_contracts(&self) -> Decimal18 {
        self.groups
            .iter()
            .fold(ZERO, |sum, group| sum + group.naked_put_contracts)
    }

    /// Total short calls with no covering instrument.
    pub fn naked_call_contracts(&self) -> Decimal18 {
        self.groups
            .iter()
            .fold(ZERO, |sum, group| sum + group.naked_call_contracts)
    }

    /// Whether any naked short option exposure remains.
    pub fn naked(&self) -> bool {
        self.naked_put_contracts().is_positive() || self.naked_call_contracts().is_positive()
    }
}

#[derive(Clone, Debug)]
struct OptionPosition {
    contract: OsiSymbol,
    contracts: Decimal18,
}

fn projected_quantities(
    order: &OrderRequest,
    context: &RiskContext,
) -> (HashMap<Symbol, Decimal18>, HashMap<Symbol, Decimal18>) {
    let mut current = HashMap::new();
    for position in &context.portfolio.positions {
        current.insert(position.symbol.clone(), position.quantity);
    }
    let mut projected = current.clone();
    let legs: Vec<(Symbol, Side, Decimal18)> = match order {
        OrderRequest::Single(single) => {
            vec![(single.symbol.clone(), single.side, single.quantity)]
        }
        OrderRequest::Multi(multi) => multi
            .legs
            .iter()
            .map(|leg| (leg.symbol.clone(), leg.side, leg.quantity))
            .collect(),
    };
    for (symbol, side, quantity) in legs {
        let signed = if side == Side::Buy { quantity } else { -quantity };
        *projected.entry(symbol).or_insert(ZERO) += signed;
    }
    (current, projected)
}

fn scenario_requirement(
    calls: &[(Decimal18, Decimal18)],
    puts: &[(Decimal18, Decimal18)],
) -> Option<Decimal18> {
    let mut slope = ZERO;
    for (_, signed) in calls {
        slope += *signed;
    }
    if slope.is_negative() {
        return None;
    }
    let mut strikes: BTreeSet<Decimal18> = BTreeSet::new();
    for (strike, _) in calls {
        strikes.insert(*strike);
    }
    for (strike, _) in puts {
        strikes.insert(*strike);
    }
    let mut at_zero = ZERO;
    for (strike, signed) in puts {
        at_zero += *signed * *strike;
    }
    let mut worst = ZERO;
    if at_zero < worst {
        worst = at_zero;
    }
    for breakpoint in &strikes {
        let mut value = ZERO;
        for (strike, signed) in calls {
            if breakpoint > strike {
                value += *signed * (*breakpoint - *strike);
            }
        }
        for (strike, signed) in puts {
            if breakpoint < strike {
                value += *signed * (*strike - *breakpoint);
            }
        }
        if value < worst {
            worst = value;
        }
    }
    if worst.is_negative() {
        Some(-worst)
    } else {
        Some(ZERO)
    }
}

fn build_groups(
    quantities: &HashMap<Symbol, Decimal18>,
    multiplier: Decimal18,
) -> Vec<OptionGroupCollateral> {
    let mut shares: HashMap<String, Decimal18> = HashMap::new();
    let mut calls: HashMap<(String, String), Vec<OptionPosition>> = HashMap::new();
    let mut puts: HashMap<(String, String), Vec<OptionPosition>> = HashMap::new();

    let mut symbols: Vec<&Symbol> = quantities.keys().collect();
    symbols.sort();
    for symbol in symbols {
        let quantity = quantities[symbol];
        if quantity == ZERO {
            continue;
        }
        match OsiSymbol::new(symbol.as_str()) {
            Err(_) => {
                shares.insert(symbol.as_str().to_string(), quantity);
            }
            Ok(contract) => {
                let key = (contract.root().to_string(), contract.expiration().to_string());
                let target = if contract.option_type() == 'C' {
                    &mut calls
                } else {
                    &mut puts
                };
                target
                    .entry(key)
                    .or_default()
                    .push(OptionPosition { contract, contracts: quantity });
            }
        }
    }

    let mut pools: HashMap<String, Decimal18> = HashMap::new();
    for (root, quantity) in &shares {
        pools.insert(root.clone(), maximum(*quantity, ZERO) / multiplier);
    }
    let mut roots: BTreeSet<String> = BTreeSet::new();
    for (root, _) in calls.keys() {
        roots.insert(root.clone());
    }
    for (root, _) in puts.keys() {
        roots.insert(root.clone());
    }
    for root in shares.keys() {
        roots.insert(root.clone());
    }

    let mut groups = Vec::new();
    for root in roots {
        let mut pool = pools.get(&root).copied().unwrap_or(ZERO);
        let mut expiries: BTreeSet<String> = BTreeSet::new();
        for (group_root, expiry) in calls.keys() {
            if *group_root == root {
                expiries.insert(expiry.clone());
            }
        }
        for (group_root, expiry) in puts.keys() {
            if *group_root == root {
                expiries.insert(expiry.clone());
            }
        }
        for expiry in expiries {
            let call_positions = calls
                .get(&(root.clone(), expiry.clone()))
                .cloned()
                .unwrap_or_default();
            let put_positions = puts
                .get(&(root.clone(), expiry.clone()))
                .cloned()
                .unwrap_or_default();

            let mut covered_by_shares: HashMap<String, Decimal18> = HashMap::new();
            let mut short_calls: Vec<&OptionPosition> = call_positions
                .iter()
                .filter(|position| position.contracts.is_negative())
                .collect();
            short_calls.sort_by(|left, right| {
                left.contract
                    .strike()
                    .cmp(&right.contract.strike())
                    .then_with(|| left.contract.as_str().cmp(right.contract.as_str()))
            });
            for position in short_calls {
                let short = -position.contracts;
                let covered = if pool < short { pool } else { short };
                if covered.is_positive() {
                    pool -= covered;
                    covered_by_shares.insert(position.contract.as_str().to_string(), covered);
                }
            }

            let has_long_calls = call_positions.iter().any(|p| p.contracts.is_positive());
            let has_long_puts = put_positions.iter().any(|p| p.contracts.is_positive());

            let mut records = Vec::new();
            let mut naked_calls = ZERO;
            let mut naked_puts = ZERO;
            let mut adjusted_calls: Vec<(Decimal18, Decimal18)> = Vec::new();
            let mut adjusted_puts: Vec<(Decimal18, Decimal18)> = Vec::new();

            let mut ordered: Vec<OptionPosition> = call_positions
                .iter()
                .chain(put_positions.iter())
                .cloned()
                .collect();
            ordered.sort_by(|left, right| {
                left.contract
                    .option_type()
                    .cmp(&right.contract.option_type())
                    .then_with(|| left.contract.strike().cmp(&right.contract.strike()))
                    .then_with(|| left.contract.as_str().cmp(right.contract.as_str()))
            });

            for position in ordered {
                let is_call = position.contract.option_type() == 'C';
                let covered = covered_by_shares
                    .get(position.contract.as_str())
                    .copied()
                    .unwrap_or(ZERO);
                let adjusted = position.contracts + covered;
                let short_remaining = maximum(-adjusted, ZERO);
                let has_long = if is_call { has_long_calls } else { has_long_puts };
                let naked = if !position.contracts.is_negative() || has_long {
                    ZERO
                } else {
                    short_remaining
                };
                if is_call {
                    naked_calls += naked;
                    adjusted_calls.push((position.contract.strike(), adjusted * multiplier));
                } else {
                    naked_puts += naked;
                    adjusted_puts.push((position.contract.strike(), adjusted * multiplier));
                }
                records.push(ContractCollateral {
                    symbol: position.contract.as_str().to_string(),
                    root: root.clone(),
                    expiration: expiry.clone(),
                    right: position.contract.option_type(),
                    projected_contracts: position.contracts,
                    covered_by_shares: covered,
                    covered_by_long_option: position.contracts.is_negative() && has_long,
                    naked_contracts: naked,
                });
            }

            let requirement = scenario_requirement(&adjusted_calls, &adjusted_puts);
            groups.push(OptionGroupCollateral {
                root: root.clone(),
                expiration: expiry,
                contracts: records,
                requirement,
                unlimited: requirement.is_none(),
                naked_put_contracts: naked_puts,
                naked_call_contracts: naked_calls,
            });
        }
    }
    groups
}

fn maximum(left: Decimal18, right: Decimal18) -> Decimal18 {
    if left > right {
        left
    } else {
        right
    }
}

/// Return the exact projected option collateral for one candidate order.
pub fn option_collateral_report(
    order: &OrderRequest,
    context: &RiskContext,
    multiplier: Decimal18,
) -> LiveResult<OptionCollateralReport> {
    if !multiplier.is_positive() {
        return Err(LiveError::Risk("multiplier must be positive".into()));
    }
    let (_, projected) = projected_quantities(order, context);
    Ok(OptionCollateralReport {
        multiplier,
        groups: build_groups(&projected, multiplier),
    })
}

fn portfolio_report(context: &RiskContext, multiplier: Decimal18) -> OptionCollateralReport {
    let mut current = HashMap::new();
    for position in &context.portfolio.positions {
        current.insert(position.symbol.clone(), position.quantity);
    }
    OptionCollateralReport {
        multiplier,
        groups: build_groups(&current, multiplier),
    }
}

fn default_multiplier() -> LiveResult<Decimal18> {
    Ok(Decimal18::from_i64(DEFAULT_MULTIPLIER))
}

/// Reject orders that create or increase naked short option exposure.
#[derive(Clone, Debug)]
pub struct NoNakedShortOptions {
    /// The stable rule identifier.
    pub rule_id: String,
    /// Contracts-to-shares multiplier.
    pub multiplier: Decimal18,
    /// Whether covered naked short puts are permitted.
    pub allow_naked_short_put: bool,
}

impl NoNakedShortOptions {
    /// Construct the rule with the default multiplier.
    pub fn new(rule_id: &str, allow_naked_short_put: bool) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            multiplier: default_multiplier()?,
            allow_naked_short_put,
        })
    }
}

impl RiskRule for NoNakedShortOptions {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let report = option_collateral_report(order, context, self.multiplier)?;
        let before = portfolio_report(context, self.multiplier);
        if report.unlimited() {
            return Ok(RuleDecision::reject(
                order.clone(),
                "no_naked_short_options leaves unlimited short option exposure",
                false,
            ));
        }
        let before_by_key: HashMap<(String, String), &OptionGroupCollateral> = before
            .groups
            .iter()
            .map(|group| ((group.root.clone(), group.expiration.clone()), group))
            .collect();
        for group in &report.groups {
            let before_naked = before_by_key
                .get(&(group.root.clone(), group.expiration.clone()))
                .map(|before_group| before_group.naked_put_contracts)
                .unwrap_or(ZERO);
            if group.naked_put_contracts > before_naked && !self.allow_naked_short_put {
                return Ok(RuleDecision::reject(
                    order.clone(),
                    "no_naked_short_options increases naked short put exposure",
                    false,
                ));
            }
        }
        Ok(RuleDecision::allow(order.clone())
            .with_metric("total_collateral", format_decimal(report.total_collateral()))
            .with_metric(
                "naked_put_contracts",
                report.naked_put_contracts().to_string(),
            )
            .with_metric(
                "naked_call_contracts",
                report.naked_call_contracts().to_string(),
            ))
    }
}

/// Cap the exact worst-case option collateral after the candidate order.
#[derive(Clone, Debug)]
pub struct OptionCollateralLimit {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The collateral cap.
    pub limit: Decimal18,
    /// Contracts-to-shares multiplier.
    pub multiplier: Decimal18,
}

impl OptionCollateralLimit {
    /// Construct the rule with the default multiplier.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
            multiplier: default_multiplier()?,
        })
    }
}

impl RiskRule for OptionCollateralLimit {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let report = option_collateral_report(order, context, self.multiplier)?;
        if report.unlimited() {
            return Ok(RuleDecision::reject(
                order.clone(),
                "option_collateral_limit has unlimited short option exposure",
                false,
            ));
        }
        let total = report.total_collateral();
        let decision = match total {
            Some(total) if total > self.limit => {
                RuleDecision::reject(order.clone(), "option_collateral_limit exceeded", false)
            }
            _ => RuleDecision::allow(order.clone()),
        };
        Ok(decision
            .with_metric("total_collateral", format_decimal(total))
            .with_metric("limit", self.limit.to_string()))
    }
}

fn format_decimal(value: Option<Decimal18>) -> String {
    value.map(|value| value.to_string()).unwrap_or_else(|| "unlimited".into())
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use harness_num::Decimal18;

    use super::{option_collateral_report, NoNakedShortOptions};
    use crate::orders::{OrderRequest, Side, SingleLegOrder, OrderType, TimeInForce};
    use crate::portfolio::hub::PortfolioHub;
    use crate::risk::models::{RiskContext, RiskRule, RiskAction};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn short_call_order() -> OrderRequest {
        OrderRequest::Single(
            SingleLegOrder::new(
                "o1",
                Symbol::new("AAPL240119C00150000").unwrap(),
                Side::Sell,
                dec("1"),
                OrderType::Market,
                TimeInForce::Day,
                None,
                None,
                None,
            )
            .unwrap(),
        )
    }

    fn context() -> RiskContext {
        let hub = PortfolioHub::new("acct", vec![]).unwrap();
        RiskContext::new(hub.snapshot(), HashMap::new(), Decimal18::ZERO, 0, None).unwrap()
    }

    #[test]
    fn naked_short_call_is_unlimited_and_rejected() {
        let context = context();
        let report = option_collateral_report(&short_call_order(), &context, dec("100")).unwrap();
        assert!(report.unlimited());
        let rule = NoNakedShortOptions::new("no-naked", false).unwrap();
        let decision = rule.evaluate(&short_call_order(), &context).unwrap();
        assert_eq!(decision.action, RiskAction::Reject);
    }

    #[test]
    fn covered_call_by_shares_is_not_naked() {
        let mut hub = PortfolioHub::new("acct", vec![]).unwrap();
        hub.create_book("b", dec("100000")).unwrap();
        // 100 underlying shares cover one short call.
        let shares = crate::accounting::Fill::new(
            "f1",
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            dec("100"),
            dec("100"),
            Decimal18::ZERO,
            None,
        )
        .unwrap();
        hub.apply_fill("b", &shares).unwrap();
        let context =
            RiskContext::new(hub.snapshot(), HashMap::new(), Decimal18::ZERO, 0, None).unwrap();
        let report = option_collateral_report(&short_call_order(), &context, dec("100")).unwrap();
        assert!(!report.naked());
        assert_eq!(report.naked_call_contracts(), Decimal18::ZERO);
    }
}
