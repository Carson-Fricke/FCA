//! Exact-decimal built-in pre-trade risk rules.

use std::collections::{BTreeSet, HashMap};

use harness_num::Decimal18;

use crate::error::{require_text, LiveError, LiveResult};
use crate::orders::{OrderRequest, Side};
use crate::portfolio::hub::AggregatePosition;
use crate::risk::models::{RiskContext, RiskRule, RuleDecision};
use crate::symbol::Symbol;

const ZERO: Decimal18 = Decimal18::ZERO;

fn one() -> Decimal18 {
    Decimal18::ONE
}

/// Return a deterministic risk price for one order symbol.
pub fn order_price(
    order: &OrderRequest,
    context: &RiskContext,
    symbol: &Symbol,
) -> Option<Decimal18> {
    if let OrderRequest::Single(single) = order {
        if let Some(limit) = single.limit_price {
            return Some(limit);
        }
        if let Some(stop) = single.stop_price {
            return Some(stop);
        }
    }
    if let Some(explicit) = context.price(symbol) {
        return Some(explicit);
    }
    position(symbol, context).and_then(|position| position.mark_price)
}

/// Return gross order notional, or `None` when a price is unavailable.
pub fn order_notional(order: &OrderRequest, context: &RiskContext) -> Option<Decimal18> {
    match order {
        OrderRequest::Single(single) => {
            let price = order_price(order, context, &single.symbol)?;
            Some(single.quantity * price)
        }
        OrderRequest::Multi(multi) => {
            let mut total = ZERO;
            for leg in &multi.legs {
                let price = order_price(order, context, &leg.symbol)?;
                total += leg.quantity * price;
            }
            Some(total)
        }
    }
}

/// Return one exact size value for single- and multi-leg orders.
pub fn order_size(order: &OrderRequest) -> Decimal18 {
    match order {
        OrderRequest::Single(single) => single.quantity,
        OrderRequest::Multi(multi) => multi
            .legs
            .iter()
            .fold(ZERO, |sum, leg| sum + leg.quantity),
    }
}

/// Scale every order leg by an exact positive ratio.
pub fn scale_order(order: &OrderRequest, ratio: Decimal18) -> LiveResult<OrderRequest> {
    if !ratio.is_positive() || ratio > one() {
        return Err(LiveError::Risk(
            "order reduction ratio must be greater than zero".into(),
        ));
    }
    Ok(match order {
        OrderRequest::Single(single) => {
            let mut scaled = single.clone();
            scaled.quantity *= ratio;
            OrderRequest::Single(scaled)
        }
        OrderRequest::Multi(multi) => {
            let mut scaled = multi.clone();
            for leg in &mut scaled.legs {
                leg.quantity *= ratio;
            }
            OrderRequest::Multi(scaled)
        }
    })
}

fn position(symbol: &Symbol, context: &RiskContext) -> Option<AggregatePosition> {
    context.portfolio.position(symbol.as_str()).ok()
}

fn position_price(
    position: Option<&AggregatePosition>,
    symbol: &Symbol,
    context: &RiskContext,
) -> Option<Decimal18> {
    if let Some(explicit) = context.price(symbol) {
        return Some(explicit);
    }
    position.and_then(|position| position.mark_price)
}

fn order_legs(order: &OrderRequest) -> Vec<(Symbol, Side, Decimal18)> {
    match order {
        OrderRequest::Single(single) => {
            vec![(single.symbol.clone(), single.side, single.quantity)]
        }
        OrderRequest::Multi(multi) => multi
            .legs
            .iter()
            .map(|leg| (leg.symbol.clone(), leg.side, leg.quantity))
            .collect(),
    }
}

type ExposureComponents = (
    HashMap<Symbol, Decimal18>,
    HashMap<Symbol, Decimal18>,
    HashMap<Symbol, Decimal18>,
    HashMap<Symbol, Decimal18>,
    Option<HashMap<Symbol, Decimal18>>,
);

fn exposure_components(order: &OrderRequest, context: &RiskContext) -> ExposureComponents {
    let mut current = HashMap::new();
    let mut long = HashMap::new();
    let mut short = HashMap::new();
    for position in &context.portfolio.positions {
        current.insert(position.symbol.clone(), position.quantity);
        long.insert(position.symbol.clone(), position.long_quantity);
        short.insert(position.symbol.clone(), position.short_quantity);
    }
    let mut deltas: HashMap<Symbol, Decimal18> = HashMap::new();
    let mut prices: HashMap<Symbol, Decimal18> = HashMap::new();
    for (symbol, side, quantity) in order_legs(order) {
        let position = position(&symbol, context);
        let Some(price) = position_price(position.as_ref(), &symbol, context) else {
            return (current, deltas, long, short, None);
        };
        prices.insert(symbol.clone(), price);
        let signed = if side == Side::Buy {
            quantity
        } else {
            -quantity
        };
        *deltas.entry(symbol).or_insert(ZERO) += signed;
    }
    for position in &context.portfolio.positions {
        if prices.contains_key(&position.symbol) {
            continue;
        }
        let Some(price) = position_price(Some(position), &position.symbol, context) else {
            return (current, deltas, long, short, None);
        };
        prices.insert(position.symbol.clone(), price);
    }
    (current, deltas, long, short, Some(prices))
}

fn gross_quantity(
    long_quantity: Decimal18,
    short_quantity: Decimal18,
    delta: Decimal18,
) -> Decimal18 {
    if !delta.is_negative() {
        long_quantity
            + nonneg(delta - short_quantity)
            + nonneg(short_quantity - delta)
    } else {
        nonneg(long_quantity + delta) + short_quantity + nonneg(-delta - long_quantity)
    }
}

fn nonneg(value: Decimal18) -> Decimal18 {
    if value.is_positive() {
        value
    } else {
        ZERO
    }
}

fn projected_gross(order: &OrderRequest, context: &RiskContext) -> Option<Decimal18> {
    let (_, deltas, long, short, prices) = exposure_components(order, context);
    let prices = prices?;
    let mut total = ZERO;
    for (symbol, price) in &prices {
        let delta = deltas.get(symbol).copied().unwrap_or(ZERO);
        total += gross_quantity(
            long.get(symbol).copied().unwrap_or(ZERO),
            short.get(symbol).copied().unwrap_or(ZERO),
            delta,
        ) * *price;
    }
    Some(total)
}

fn projected_net(order: &OrderRequest, context: &RiskContext) -> Option<Decimal18> {
    let (current, deltas, _, _, prices) = exposure_components(order, context);
    let prices = prices?;
    let mut total = ZERO;
    for (symbol, price) in &prices {
        let quantity = current.get(symbol).copied().unwrap_or(ZERO)
            + deltas.get(symbol).copied().unwrap_or(ZERO);
        total += quantity * *price;
    }
    Some(total.abs())
}

fn projected_position(order: &OrderRequest, context: &RiskContext) -> Option<Decimal18> {
    let (current, deltas, _, _, prices) = exposure_components(order, context);
    let prices = prices?;
    let mut best = ZERO;
    for symbol in deltas.keys() {
        let quantity = current.get(symbol).copied().unwrap_or(ZERO)
            + deltas.get(symbol).copied().unwrap_or(ZERO);
        let value = quantity.abs() * prices[symbol];
        if value > best {
            best = value;
        }
    }
    Some(best)
}

fn reduced_by_notional(
    order: &OrderRequest,
    capacity: Decimal18,
    context: &RiskContext,
) -> LiveResult<Option<OrderRequest>> {
    let Some(current) = order_notional(order, context) else {
        return Ok(None);
    };
    if !capacity.is_positive() {
        return Ok(None);
    }
    if capacity >= current {
        return Ok(Some(order.clone()));
    }
    Ok(Some(scale_order(order, capacity / current)?))
}

fn max_valid_scale(
    order: &OrderRequest,
    capacity: Decimal18,
    context: &RiskContext,
    exposure: &str,
) -> Option<Decimal18> {
    if capacity.is_negative() {
        return None;
    }
    let (current, deltas, long, short, prices) = exposure_components(order, context);
    let prices = prices?;

    let value = |scale: Decimal18| -> Decimal18 {
        if exposure == "gross" {
            let mut total = ZERO;
            for symbol in prices.keys() {
                let delta = deltas.get(symbol).copied().unwrap_or(ZERO) * scale;
                total += gross_quantity(
                    long.get(symbol).copied().unwrap_or(ZERO),
                    short.get(symbol).copied().unwrap_or(ZERO),
                    delta,
                ) * prices[symbol];
            }
            total
        } else if exposure == "net" {
            let mut total = ZERO;
            for symbol in prices.keys() {
                let quantity =
                    current.get(symbol).copied().unwrap_or(ZERO) + deltas.get(symbol).copied().unwrap_or(ZERO) * scale;
                total += quantity * prices[symbol];
            }
            total.abs()
        } else if exposure == "position" {
            let mut best = ZERO;
            for symbol in deltas.keys() {
                let quantity =
                    current.get(symbol).copied().unwrap_or(ZERO) + deltas.get(symbol).copied().unwrap_or(ZERO) * scale;
                let value = quantity.abs() * prices[symbol];
                if value > best {
                    best = value;
                }
            }
            best
        } else {
            ZERO
        }
    };

    if value(one()) <= capacity {
        return Some(one());
    }
    let mut points: BTreeSet<Decimal18> = BTreeSet::new();
    points.insert(ZERO);
    points.insert(one());
    for (symbol, delta) in &deltas {
        if *delta == ZERO {
            continue;
        }
        let crossing = if exposure == "gross" {
            if delta.is_positive() {
                -short.get(symbol).copied().unwrap_or(ZERO) / *delta
            } else {
                long.get(symbol).copied().unwrap_or(ZERO) / -*delta
            }
        } else {
            -current.get(symbol).copied().unwrap_or(ZERO) / *delta
        };
        if crossing.is_positive() && crossing < one() {
            points.insert(crossing);
        }
    }
    if exposure == "net" {
        let mut current_value = ZERO;
        let mut delta_value = ZERO;
        for symbol in prices.keys() {
            current_value += current.get(symbol).copied().unwrap_or(ZERO) * prices[symbol];
            delta_value += deltas.get(symbol).copied().unwrap_or(ZERO) * prices[symbol];
        }
        if delta_value != ZERO {
            let crossing = -current_value / delta_value;
            if crossing.is_positive() && crossing < one() {
                points.insert(crossing);
            }
        }
    }

    let mut best = ZERO;
    let ordered: Vec<Decimal18> = points.into_iter().collect();
    for window in ordered.windows(2) {
        let (left, right) = (window[0], window[1]);
        let left_value = value(left);
        let right_value = value(right);
        if right_value <= capacity {
            if right > best {
                best = right;
            }
            continue;
        }
        if left_value > capacity {
            continue;
        }
        let slope = (right_value - left_value) / (right - left);
        if slope != ZERO {
            let crossing = left + (capacity - left_value) / slope;
            if left < crossing && crossing < right && value(crossing) <= capacity && crossing > best {
                best = crossing;
            }
        }
    }
    if best.is_positive() {
        Some(best)
    } else {
        None
    }
}

fn metric(value: Decimal18) -> String {
    value.to_string()
}

/// Cap the gross notional of one candidate order.
#[derive(Clone, Debug)]
pub struct MaxOrderNotional {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The notional cap.
    pub limit: Decimal18,
}

impl MaxOrderNotional {
    /// Construct the rule.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for MaxOrderNotional {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let Some(notional) = order_notional(order, context) else {
            return Ok(RuleDecision::reject(
                order.clone(),
                "max_order_notional missing price",
                false,
            ));
        };
        let decision = if notional <= self.limit {
            RuleDecision::allow(order.clone())
        } else {
            match reduced_by_notional(order, self.limit, context)? {
                None => RuleDecision::reject(
                    order.clone(),
                    "max_order_notional has no permitted quantity",
                    false,
                ),
                Some(reduced) => {
                    RuleDecision::reduce(reduced, "max_order_notional reduced order")
                }
            }
        };
        Ok(decision
            .with_metric("notional", metric(notional))
            .with_metric("limit", metric(self.limit)))
    }
}

/// Cap the resulting notional of each changed position.
#[derive(Clone, Debug)]
pub struct MaxPositionNotional {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The position notional cap.
    pub limit: Decimal18,
}

impl MaxPositionNotional {
    /// Construct the rule.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for MaxPositionNotional {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let Some(max_position) = projected_position(order, context) else {
            return Ok(RuleDecision::reject(
                order.clone(),
                "max_position_notional missing price",
                false,
            ));
        };
        let decision = if max_position <= self.limit {
            RuleDecision::allow(order.clone())
        } else {
            match max_valid_scale(order, self.limit, context, "position") {
                None => RuleDecision::reject(
                    order.clone(),
                    "max_position_notional has no permitted quantity",
                    false,
                ),
                Some(scale) => RuleDecision::reduce(
                    scale_order(order, scale)?,
                    "max_position_notional reduced order",
                ),
            }
        };
        Ok(decision
            .with_metric("position_notional", metric(max_position))
            .with_metric("limit", metric(self.limit)))
    }
}

/// Cap gross marked exposure after the candidate order.
#[derive(Clone, Debug)]
pub struct MaxGrossExposure {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The gross exposure cap.
    pub limit: Decimal18,
}

impl MaxGrossExposure {
    /// Construct the rule.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for MaxGrossExposure {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let Some(projected) = projected_gross(order, context) else {
            return Ok(RuleDecision::reject(
                order.clone(),
                "max_gross_exposure missing price",
                false,
            ));
        };
        let decision = if projected <= self.limit {
            RuleDecision::allow(order.clone())
        } else {
            match max_valid_scale(order, self.limit, context, "gross") {
                None => RuleDecision::reject(
                    order.clone(),
                    "max_gross_exposure has no permitted quantity",
                    false,
                ),
                Some(scale) => RuleDecision::reduce(
                    scale_order(order, scale)?,
                    "max_gross_exposure reduced order",
                ),
            }
        };
        Ok(decision
            .with_metric("gross_exposure", metric(projected))
            .with_metric("limit", metric(self.limit)))
    }
}

/// Cap absolute marked net exposure after the candidate order.
#[derive(Clone, Debug)]
pub struct MaxNetExposure {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The net exposure cap.
    pub limit: Decimal18,
}

impl MaxNetExposure {
    /// Construct the rule.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for MaxNetExposure {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let Some(projected) = projected_net(order, context) else {
            return Ok(RuleDecision::reject(
                order.clone(),
                "max_net_exposure missing price",
                false,
            ));
        };
        let decision = if projected <= self.limit {
            RuleDecision::allow(order.clone())
        } else {
            match max_valid_scale(order, self.limit, context, "net") {
                None => RuleDecision::reject(
                    order.clone(),
                    "max_net_exposure has no permitted quantity",
                    false,
                ),
                Some(scale) => {
                    RuleDecision::reduce(scale_order(order, scale)?, "max_net_exposure reduced order")
                }
            }
        };
        Ok(decision
            .with_metric("net_exposure", metric(projected))
            .with_metric("limit", metric(self.limit)))
    }
}

/// Cap the quantity of a single- or multi-leg order.
#[derive(Clone, Debug)]
pub struct MaxOrderSize {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The size cap.
    pub limit: Decimal18,
}

impl MaxOrderSize {
    /// Construct the rule.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for MaxOrderSize {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, _context: &RiskContext) -> LiveResult<RuleDecision> {
        let size = order_size(order);
        let decision = if size <= self.limit {
            RuleDecision::allow(order.clone())
        } else {
            let reduced = if self.limit.is_positive() {
                Some(scale_order(order, self.limit / size)?)
            } else {
                None
            };
            match reduced {
                None => RuleDecision::reject(
                    order.clone(),
                    "max_order_size has no permitted quantity",
                    false,
                ),
                Some(reduced) => RuleDecision::reduce(reduced, "max_order_size reduced order"),
            }
        };
        Ok(decision
            .with_metric("order_size", metric(size))
            .with_metric("limit", metric(self.limit)))
    }
}

/// Reject a candidate when the explicit order count is exhausted.
#[derive(Clone, Debug)]
pub struct MaxOrderCount {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The order count cap.
    pub limit: Decimal18,
}

impl MaxOrderCount {
    /// Construct the rule, requiring an integral cap.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        let integral = limit
            .to_i128(harness_num::RoundingMode::Truncate)
            .map_err(|_| LiveError::Risk("invalid order count limit".into()))?;
        if Decimal18::from_i128(integral)
            .map(|value| value != limit)
            .unwrap_or(true)
        {
            return Err(LiveError::Risk(
                "max_order_count limit must be an integer".into(),
            ));
        }
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for MaxOrderCount {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let count = Decimal18::from_i64(context.order_count as i64);
        let decision = if count < self.limit {
            RuleDecision::allow(order.clone())
        } else {
            RuleDecision::reject(order.clone(), "max_order_count limit reached", false)
        };
        Ok(decision
            .with_metric("order_count", context.order_count.to_string())
            .with_metric("limit", metric(self.limit)))
    }
}

/// Reject and mark a halt when explicit daily loss reaches its limit.
#[derive(Clone, Debug)]
pub struct DailyLossHalt {
    /// The stable rule identifier.
    pub rule_id: String,
    /// The loss cap.
    pub limit: Decimal18,
}

impl DailyLossHalt {
    /// Construct the rule.
    pub fn new(rule_id: &str, limit: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            rule_id: require_text(rule_id, "rule_id")?,
            limit,
        })
    }
}

impl RiskRule for DailyLossHalt {
    fn rule_id(&self) -> &str {
        &self.rule_id
    }

    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision> {
        let loss = nonneg(context.daily_loss);
        let decision = if loss < self.limit {
            RuleDecision::allow(order.clone())
        } else {
            RuleDecision::reject(order.clone(), "daily_loss_halt limit reached", true)
        };
        Ok(decision
            .with_metric("daily_loss", metric(loss))
            .with_metric("limit", metric(self.limit)))
    }
}
