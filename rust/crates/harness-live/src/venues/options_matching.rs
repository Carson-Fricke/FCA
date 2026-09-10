//! Atomic multi-leg options matching on constituent quote updates.
//!
//! Every group fills only when every leg is fillable at its full leg quantity
//! on one constituent quote update; partial fills never split legs. Quotes
//! arrive per constituent symbol, so each leg is evaluated against the venue's
//! latest quote for that symbol. Market groups fill at the far touch; limit
//! groups fill every leg at the exact on-tick midpoint when the natural
//! far-touch combination is marketable through the group limit.

use std::collections::HashMap;

use harness_num::Decimal18;

use crate::accounting::Fill;
use crate::error::{require_text, LiveError, LiveResult};
use crate::events::QuoteEvent;
use crate::fills::mid_touch::MidTouch;
use crate::oms::groups::{GroupOrderType, GroupSnapshot, MultiLegOms, MultiLegOrderGroup};
use crate::orders::{MultiLegOrder, OrderLeg, Side, TimeInForce};
use crate::symbol::Symbol;
use crate::venues::scripted::VenueOutcome;

const ZERO: Decimal18 = Decimal18::ZERO;

struct WorkingGroup {
    group: MultiLegOrderGroup,
    order_type: GroupOrderType,
}

struct LegCandidate {
    leg: OrderLeg,
    quote: QuoteEvent,
    price: Decimal18,
}

/// Match atomic multi-leg option groups against constituent quotes.
pub struct OptionsMatchingEngine {
    fill_model: MidTouch,
    venue: String,
    groups: HashMap<String, WorkingGroup>,
    group_ids: Vec<String>,
    used_group_ids: std::collections::HashSet<String>,
    latest_quotes: HashMap<Symbol, QuoteEvent>,
    available: HashMap<Symbol, HashMap<Side, Decimal18>>,
    oms: Option<MultiLegOms>,
    outcomes: Vec<VenueOutcome>,
    ordinal: usize,
}

impl OptionsMatchingEngine {
    /// Construct an engine with a midpoint fill model.
    pub fn new(venue: &str, fill_model: MidTouch) -> LiveResult<Self> {
        Ok(Self {
            fill_model,
            venue: require_text(venue, "venue")?,
            groups: HashMap::new(),
            group_ids: Vec::new(),
            used_group_ids: std::collections::HashSet::new(),
            latest_quotes: HashMap::new(),
            available: HashMap::new(),
            oms: None,
            outcomes: Vec::new(),
            ordinal: 0,
        })
    }

    /// Attach an OMS whose lifecycle mirrors engine decisions.
    pub fn with_oms(mut self, oms: MultiLegOms) -> Self {
        self.oms = Some(oms);
        self
    }

    /// The latest quote per constituent symbol in feed order.
    pub fn latest_quotes(&self) -> Vec<&QuoteEvent> {
        self.latest_quotes.values().collect()
    }

    /// Outcomes scheduled so far.
    pub fn outcomes(&self) -> &[VenueOutcome] {
        &self.outcomes
    }

    /// Drain the scheduled outcomes.
    pub fn drain_outcomes(&mut self) -> Vec<VenueOutcome> {
        std::mem::take(&mut self.outcomes)
    }

    /// Accept a multi-leg group and evaluate it against known quotes.
    pub fn submit(
        &mut self,
        order: MultiLegOrder,
        order_type: Option<GroupOrderType>,
    ) -> LiveResult<()> {
        if self.used_group_ids.contains(&order.order_id) {
            return Err(LiveError::Venue(format!(
                "group order_id {:?} was already used",
                order.order_id
            )));
        }
        let mut group = MultiLegOrderGroup::new(order.clone(), order_type)?;
        group.accept()?;
        self.used_group_ids.insert(order.order_id.clone());
        let resolved_type = group.snapshot().order_type;
        let working = WorkingGroup {
            group,
            order_type: resolved_type,
        };
        self.groups.insert(order.order_id.clone(), working);
        self.group_ids.push(order.order_id.clone());
        if let Some(oms) = self.oms.as_mut() {
            oms.submit(order.clone(), order_type)?;
            oms.accept(&order.order_id)?;
        }
        let filled = self.evaluate(&order.order_id)?;
        if !filled && order.time_in_force == TimeInForce::Ioc {
            self.expire(&order.order_id)?;
        }
        Ok(())
    }

    /// Cancel an active group and propagate cancellation to the OMS.
    pub fn cancel(&mut self, group_id: &str) -> LiveResult<()> {
        self.group_mut(group_id)?.group.cancel(None)?;
        if let Some(oms) = self.oms.as_mut() {
            oms.cancel(group_id, None)?;
        }
        Ok(())
    }

    /// Return the immutable engine-side snapshot for a group.
    pub fn state(&self, group_id: &str) -> LiveResult<GroupSnapshot> {
        Ok(self.group(group_id)?.group.snapshot().clone())
    }

    /// Return group snapshots in deterministic submission order.
    pub fn states(&self) -> Vec<GroupSnapshot> {
        self.group_ids
            .iter()
            .filter_map(|id| self.groups.get(id))
            .map(|working| working.group.snapshot().clone())
            .collect()
    }

    /// Return the committed atomic fill batch in original leg order.
    pub fn fills_for(&self, group_id: &str) -> LiveResult<Vec<Fill>> {
        Ok(self.group(group_id)?.group.fills().to_vec())
    }

    /// Return the latest quote observed for one symbol.
    pub fn latest_quote(&self, symbol: &str) -> LiveResult<Option<&QuoteEvent>> {
        Ok(self.latest_quotes.get(&Symbol::new(symbol)?))
    }

    /// Feed one quote event into the matching engine.
    pub fn on_quote(&mut self, event: QuoteEvent) -> LiveResult<()> {
        self.latest_quotes.insert(event.symbol.clone(), event.clone());
        let mut pool = HashMap::new();
        pool.insert(Side::Buy, event.ask_quantity);
        pool.insert(Side::Sell, event.bid_quantity);
        self.available.insert(event.symbol.clone(), pool);

        let group_ids = self.group_ids.clone();
        for group_id in group_ids {
            let active = self
                .groups
                .get(&group_id)
                .is_some_and(|working| working.group.snapshot().is_active());
            if !active {
                continue;
            }
            let contains = self
                .groups
                .get(&group_id)
                .is_some_and(|working| {
                    working
                        .group
                        .order()
                        .legs
                        .iter()
                        .any(|leg| leg.symbol == event.symbol)
                });
            if contains {
                self.evaluate(&group_id)?;
            }
        }
        Ok(())
    }

    fn evaluate(&mut self, group_id: &str) -> LiveResult<bool> {
        let Some(working) = self.groups.get(group_id) else {
            return Ok(false);
        };
        let order_type = working.order_type;
        let legs = working.group.order().legs.clone();
        let mut candidate = Vec::with_capacity(legs.len());
        for leg in legs {
            let Some(quote) = self.latest_quotes.get(&leg.symbol).cloned() else {
                return Ok(false);
            };
            let Some(pool) = self.available.get(&leg.symbol) else {
                return Ok(false);
            };
            if pool.get(&leg.side).copied().unwrap_or(ZERO) < leg.quantity {
                return Ok(false);
            }
            let Some(price) = self.leg_price(order_type, &leg, &quote)? else {
                return Ok(false);
            };
            candidate.push(LegCandidate { leg, quote, price });
        }
        let order = self.group(group_id)?.group.order().clone();
        if !self.net_limit(order_type, &order, &candidate) {
            return Ok(false);
        }
        Ok(self.commit(group_id, &candidate)?.is_some())
    }

    fn leg_price(
        &self,
        order_type: GroupOrderType,
        leg: &OrderLeg,
        quote: &QuoteEvent,
    ) -> LiveResult<Option<Decimal18>> {
        if order_type == GroupOrderType::Market {
            return Ok(Some(touch_price(leg, quote)));
        }
        self.fill_model.mid_execution(quote)
    }

    fn net_limit(
        &self,
        order_type: GroupOrderType,
        order: &MultiLegOrder,
        candidate: &[LegCandidate],
    ) -> bool {
        if order_type == GroupOrderType::Market {
            return true;
        }
        let Some(limit) = order.limit_price else {
            return false;
        };
        let natural = net(candidate.iter().map(|entry| {
            (entry.leg.clone(), touch_price(&entry.leg, &entry.quote))
        }));
        let mid_net = net(candidate
            .iter()
            .map(|entry| (entry.leg.clone(), entry.price)));
        if order_type == GroupOrderType::NetDebit {
            return !(natural > limit || mid_net <= ZERO || mid_net > limit);
        }
        !(natural > -limit || mid_net >= ZERO || -mid_net < limit)
    }

    fn commit(
        &mut self,
        group_id: &str,
        candidate: &[LegCandidate],
    ) -> LiveResult<Option<Vec<Fill>>> {
        let (group_order_id, order_type, _legs) = {
            let working = self.group(group_id)?;
            (
                working.group.order().order_id.clone(),
                working.order_type,
                working.group.order().legs.clone(),
            )
        };
        let liquidity = if order_type == GroupOrderType::Market {
            "taker"
        } else {
            "maker"
        };
        let mut fills = Vec::with_capacity(candidate.len());
        for entry in candidate {
            let fill_id = self.next_id("fill");
            fills.push(Fill::new(
                &fill_id,
                &group_order_id,
                entry.leg.symbol.clone(),
                entry.leg.side,
                entry.leg.quantity,
                entry.price,
                ZERO,
                Some(liquidity.to_string()),
            )?);
        }

        {
            let working = self.group_mut(group_id)?;
            working.group.begin_evaluation()?;
            if working.group.fill(fills.clone()).is_err() {
                working.group.end_evaluation()?;
                return Ok(None);
            }
        }
        if let Some(oms) = self.oms.as_mut() {
            oms.begin_evaluation(&group_order_id)?;
            oms.fill(&group_order_id, fills.clone())?;
        }
        for entry in candidate {
            if let Some(pool) = self.available.get_mut(&entry.leg.symbol) {
                if let Some(available) = pool.get_mut(&entry.leg.side) {
                    *available -= entry.leg.quantity;
                }
            }
        }
        for fill in &fills {
            self.outcomes.push(VenueOutcome::Fill(fill.clone()));
        }
        Ok(Some(fills))
    }

    fn expire(&mut self, group_id: &str) -> LiveResult<()> {
        self.group_mut(group_id)?.group.expire(None)?;
        if let Some(oms) = self.oms.as_mut() {
            oms.expire(group_id, None)?;
        }
        Ok(())
    }

    fn group(&self, group_id: &str) -> LiveResult<&WorkingGroup> {
        self.groups
            .get(group_id)
            .ok_or_else(|| LiveError::Venue(format!("unknown options group {group_id:?}")))
    }

    fn group_mut(&mut self, group_id: &str) -> LiveResult<&mut WorkingGroup> {
        self.groups
            .get_mut(group_id)
            .ok_or_else(|| LiveError::Venue(format!("unknown options group {group_id:?}")))
    }

    fn next_id(&mut self, kind: &str) -> String {
        self.ordinal += 1;
        format!("{}-{}-{}", self.venue, kind, self.ordinal)
    }
}

fn touch_price(leg: &OrderLeg, quote: &QuoteEvent) -> Decimal18 {
    if leg.side == Side::Buy {
        quote.ask_price
    } else {
        quote.bid_price
    }
}

fn net(terms: impl Iterator<Item = (OrderLeg, Decimal18)>) -> Decimal18 {
    let mut total = ZERO;
    for (leg, price) in terms {
        let weighted = price * leg.ratio;
        total = if leg.side == Side::Buy {
            total + weighted
        } else {
            total - weighted
        };
    }
    total
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::OptionsMatchingEngine;
    use crate::events::QuoteEvent;
    use crate::fills::mid_touch::MidTouch;
    use crate::oms::groups::GroupOrderType;
    use crate::orders::{MultiLegOrder, OrderLeg, OrderType, Side, TimeInForce};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn leg(symbol: &str, side: Side) -> OrderLeg {
        OrderLeg::new(Symbol::new(symbol).unwrap(), side, dec("1"), dec("1"), None).unwrap()
    }

    fn spread(order_id: &str, order_type: OrderType, limit: Option<&str>) -> MultiLegOrder {
        MultiLegOrder::new(
            order_id,
            vec![leg("AAPL", Side::Buy), leg("MSFT", Side::Sell)],
            order_type,
            TimeInForce::Day,
            None,
            limit.map(dec),
        )
        .unwrap()
    }

    fn quote(symbol: &str, bid: &str, ask: &str, qty: &str) -> QuoteEvent {
        QuoteEvent::new(
            Symbol::new(symbol).unwrap(),
            dec(bid),
            dec(qty),
            dec(ask),
            dec(qty),
            None,
        )
        .unwrap()
    }

    #[test]
    fn market_group_fills_atomically_when_all_legs_quoted() {
        let mut engine =
            OptionsMatchingEngine::new("options", MidTouch::default()).unwrap();
        engine.on_quote(quote("AAPL", "100", "101", "5")).unwrap();
        engine.submit(spread("g1", OrderType::Market, None), None).unwrap();
        assert_eq!(engine.state("g1").unwrap().status, crate::oms::groups::GroupStatus::Accepted);
        engine.on_quote(quote("MSFT", "99", "100", "5")).unwrap();
        let snapshot = engine.state("g1").unwrap();
        assert_eq!(snapshot.status, crate::oms::groups::GroupStatus::Filled);
        assert_eq!(engine.fills_for("g1").unwrap().len(), 2);
    }

    #[test]
    fn ioc_group_expires_without_a_complete_cross() {
        let mut engine =
            OptionsMatchingEngine::new("options", MidTouch::default()).unwrap();
        let order = MultiLegOrder::new(
            "g2",
            vec![leg("AAPL", Side::Buy), leg("MSFT", Side::Sell)],
            OrderType::Market,
            TimeInForce::Ioc,
            None,
            None,
        )
        .unwrap();
        engine.submit(order, None).unwrap();
        assert_eq!(
            engine.state("g2").unwrap().status,
            crate::oms::groups::GroupStatus::Expired
        );
    }

    #[test]
    fn net_debit_limit_requires_net_debit() {
        let mut engine =
            OptionsMatchingEngine::new("options", MidTouch::default()).unwrap();
        engine
            .submit(
                spread("g3", OrderType::Limit, Some("3")),
                Some(GroupOrderType::NetDebit),
            )
            .unwrap();
        engine.on_quote(quote("AAPL", "101", "102", "5")).unwrap();
        engine.on_quote(quote("MSFT", "99", "100", "5")).unwrap();
        let snapshot = engine.state("g3").unwrap();
        assert_eq!(snapshot.status, crate::oms::groups::GroupStatus::Filled);
        assert_eq!(snapshot.net_price.unwrap(), dec("2"));
    }
}
