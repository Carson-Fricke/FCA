//! Atomic lifecycle and exact accounting for multi-leg order groups.

use std::collections::{HashMap, HashSet};

use num::Decimal18;

use crate::accounting::Fill;
use crate::error::{LiveError, LiveResult};
use crate::orders::{MultiLegOrder, OrderLeg, OrderType, Side};
use crate::symbol::Symbol;

const ZERO: Decimal18 = Decimal18::ZERO;

/// Price constraint applied to an atomic multi-leg order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum GroupOrderType {
    /// No net-price constraint.
    Market,
    /// The group must execute for a positive net debit.
    NetDebit,
    /// The group must execute for a negative net credit.
    NetCredit,
}

/// Lifecycle state shared by a group and every one of its legs.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum GroupStatus {
    /// Locally new.
    New,
    /// Acknowledged.
    Accepted,
    /// Being considered for atomic execution.
    Evaluating,
    /// Fully filled.
    Filled,
    /// Canceled.
    Canceled,
    /// Rejected.
    Rejected,
    /// Expired.
    Expired,
}

impl GroupStatus {
    fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Filled | Self::Canceled | Self::Rejected | Self::Expired
        )
    }
}

/// Lifecycle and exact execution ledger for one ordered group leg.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GroupLegSnapshot {
    /// Original leg index.
    pub index: usize,
    /// The immutable leg.
    pub leg: OrderLeg,
    /// Current lifecycle status.
    pub status: GroupStatus,
    /// Filled quantity.
    pub filled_quantity: Decimal18,
    /// Remaining quantity.
    pub remaining_quantity: Decimal18,
    /// Filled notional.
    pub filled_notional: Decimal18,
    /// Average fill price.
    pub average_fill_price: Option<Decimal18>,
    /// Fee.
    pub fee: Decimal18,
    /// Signed gross cash.
    pub gross_cash: Decimal18,
    /// Gross cash minus fee.
    pub net_cash: Decimal18,
    /// Signed position delta.
    pub position_quantity: Decimal18,
    /// The committed fill, when filled.
    pub fill: Option<Fill>,
}

impl GroupLegSnapshot {
    /// The optional venue-facing leg identifier.
    pub fn leg_id(&self) -> Option<&str> {
        self.leg.leg_id.as_deref()
    }

    /// The buy or sell action for this leg.
    pub fn action(&self) -> Side {
        self.leg.side
    }

    /// The leg's ratio used for group net-price accounting.
    pub fn ratio(&self) -> Decimal18 {
        self.leg.ratio
    }

    /// The exact quantity that an atomic fill must execute.
    pub fn quantity(&self) -> Decimal18 {
        self.leg.quantity
    }

    /// The canonical symbol for this leg.
    pub fn symbol(&self) -> &Symbol {
        &self.leg.symbol
    }
}

/// Immutable lifecycle and accounting snapshot for one order group.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GroupSnapshot {
    /// The immutable multi-leg order request.
    pub order: MultiLegOrder,
    /// The resolved group order type.
    pub order_type: GroupOrderType,
    /// Current lifecycle status.
    pub status: GroupStatus,
    /// Per-leg snapshots.
    pub legs: Vec<GroupLegSnapshot>,
    /// Ratio-weighted net price: positive debit, negative credit.
    pub net_price: Option<Decimal18>,
    /// Signed gross cash across legs.
    pub gross_cash: Decimal18,
    /// Total fees across legs.
    pub fees: Decimal18,
    /// Gross cash minus fees.
    pub net_cash: Decimal18,
    /// Number of lifecycle transitions.
    pub transition_count: usize,
    /// Optional termination reason.
    pub reason: Option<String>,
}

impl GroupSnapshot {
    /// The multi-leg order identifier.
    pub fn group_id(&self) -> &str {
        &self.order.order_id
    }

    /// The common group quantity implied by every leg ratio.
    pub fn quantity(&self) -> Decimal18 {
        let first = &self.legs[0];
        first.quantity() / first.ratio()
    }

    /// Whether the group can accept another lifecycle operation.
    pub fn is_active(&self) -> bool {
        !self.status.is_terminal()
    }

    /// Whether the group has reached an irreversible state.
    pub fn is_terminal(&self) -> bool {
        self.status.is_terminal()
    }

    /// Committed fills in original leg order.
    pub fn fills(&self) -> Vec<Fill> {
        self.legs
            .iter()
            .filter_map(|leg| leg.fill.clone())
            .collect()
    }

    /// The positive ratio-weighted debit, or zero for a credit.
    pub fn net_debit(&self) -> Decimal18 {
        match self.net_price {
            None => ZERO,
            Some(price) if price > ZERO => price,
            Some(_) => ZERO,
        }
    }

    /// The positive ratio-weighted credit, or zero for a debit.
    pub fn net_credit(&self) -> Decimal18 {
        match self.net_price {
            None => ZERO,
            Some(price) if price < ZERO => -price,
            Some(_) => ZERO,
        }
    }
}

/// Apply an all-or-nothing lifecycle to one immutable multi-leg order.
#[derive(Clone, Debug)]
pub struct MultiLegOrderGroup {
    snapshot: GroupSnapshot,
    fills: Vec<Fill>,
}

impl MultiLegOrderGroup {
    /// Construct a validated group in `NEW` state.
    pub fn new(order: MultiLegOrder, order_type: Option<GroupOrderType>) -> LiveResult<Self> {
        let resolved_type = Self::resolve_order_type(&order, order_type)?;
        let mut seen = HashSet::new();
        for leg in &order.legs {
            if let Some(leg_id) = &leg.leg_id {
                if !seen.insert(leg_id.clone()) {
                    return Err(LiveError::GroupTransition(
                        "leg_id values must be unique within a group".into(),
                    ));
                }
            }
        }
        let first = &order.legs[0];
        let group_quantity = first.quantity / first.ratio;
        for leg in &order.legs[1..] {
            if leg.quantity / leg.ratio != group_quantity {
                return Err(LiveError::GroupTransition(
                    "leg quantities must be proportional to their ratios".into(),
                ));
            }
        }

        let legs = order
            .legs
            .iter()
            .enumerate()
            .map(|(index, leg)| GroupLegSnapshot {
                index,
                leg: leg.clone(),
                status: GroupStatus::New,
                filled_quantity: ZERO,
                remaining_quantity: leg.quantity,
                filled_notional: ZERO,
                average_fill_price: None,
                fee: ZERO,
                gross_cash: ZERO,
                net_cash: ZERO,
                position_quantity: ZERO,
                fill: None,
            })
            .collect();

        Ok(Self {
            snapshot: GroupSnapshot {
                order,
                order_type: resolved_type,
                status: GroupStatus::New,
                legs,
                net_price: None,
                gross_cash: ZERO,
                fees: ZERO,
                net_cash: ZERO,
                transition_count: 0,
                reason: None,
            },
            fills: Vec::new(),
        })
    }

    /// The current immutable group snapshot.
    pub fn snapshot(&self) -> &GroupSnapshot {
        &self.snapshot
    }

    /// The immutable multi-leg order request.
    pub fn order(&self) -> &MultiLegOrder {
        &self.snapshot.order
    }

    /// Committed fills in original leg order.
    pub fn fills(&self) -> &[Fill] {
        &self.fills
    }

    /// Acknowledge a locally new group.
    pub fn accept(&mut self) -> LiveResult<GroupSnapshot> {
        self.require_status("accept", &[GroupStatus::New])?;
        Ok(self.transition(GroupStatus::Accepted, None))
    }

    /// Mark an accepted group as being considered for atomic execution.
    pub fn begin_evaluation(&mut self) -> LiveResult<GroupSnapshot> {
        self.require_status("begin evaluation", &[GroupStatus::Accepted])?;
        Ok(self.transition(GroupStatus::Evaluating, None))
    }

    /// Return an unfilled evaluated group to its accepted working state.
    pub fn end_evaluation(&mut self) -> LiveResult<GroupSnapshot> {
        self.require_status("end evaluation", &[GroupStatus::Evaluating])?;
        Ok(self.transition(GroupStatus::Accepted, None))
    }

    /// Validate and atomically commit exactly one complete fill per leg.
    pub fn fill(&mut self, fills: Vec<Fill>) -> LiveResult<GroupSnapshot> {
        self.require_status("fill", &[GroupStatus::Evaluating])?;
        let ordered = self.ordered_complete_fills(&fills)?;
        let next = self.filled_snapshot(&ordered)?;
        self.fills = ordered;
        self.snapshot = next.clone();
        Ok(next)
    }

    /// Cancel an active group and propagate cancellation to every leg.
    pub fn cancel(&mut self, reason: Option<String>) -> LiveResult<GroupSnapshot> {
        self.require_active("cancel")?;
        let reason = Self::normalize_reason(reason)?;
        Ok(self.transition(GroupStatus::Canceled, reason))
    }

    /// Reject an active group and propagate rejection to every leg.
    pub fn reject(&mut self, reason: Option<String>) -> LiveResult<GroupSnapshot> {
        self.require_active("reject")?;
        let reason = Self::normalize_reason(reason)?;
        Ok(self.transition(GroupStatus::Rejected, reason))
    }

    /// Expire a working group and propagate expiry to every leg.
    pub fn expire(&mut self, reason: Option<String>) -> LiveResult<GroupSnapshot> {
        self.require_status("expire", &[GroupStatus::Accepted, GroupStatus::Evaluating])?;
        let reason = Self::normalize_reason(reason)?;
        Ok(self.transition(GroupStatus::Expired, reason))
    }

    fn transition(&mut self, status: GroupStatus, reason: Option<String>) -> GroupSnapshot {
        let legs = self
            .snapshot
            .legs
            .iter()
            .map(|leg| {
                let mut next = leg.clone();
                next.status = status;
                next
            })
            .collect();
        let next = GroupSnapshot {
            status,
            legs,
            transition_count: self.snapshot.transition_count + 1,
            reason,
            ..self.snapshot.clone()
        };
        self.snapshot = next.clone();
        next
    }

    fn ordered_complete_fills(&self, candidate: &[Fill]) -> LiveResult<Vec<Fill>> {
        let legs = &self.snapshot.legs;
        if candidate.len() != legs.len() {
            return Err(LiveError::GroupFill(
                "atomic group fill requires exactly one fill for every leg".into(),
            ));
        }
        let mut fill_ids = HashSet::new();
        for fill in candidate {
            if !fill_ids.insert(fill.fill_id.clone()) {
                return Err(LiveError::GroupFill(
                    "fill_id values must be unique within a group fill".into(),
                ));
            }
        }
        if candidate
            .iter()
            .any(|fill| fill.order_id != self.snapshot.group_id())
        {
            return Err(LiveError::GroupFill(
                "every fill must reference the group order_id".into(),
            ));
        }

        let mut unmatched: Vec<Fill> = candidate.to_vec();
        let mut ordered = Vec::with_capacity(legs.len());
        for leg in legs {
            let match_index = unmatched.iter().position(|fill| {
                fill.symbol == leg.leg.symbol
                    && fill.side == leg.action()
                    && fill.quantity == leg.quantity()
            });
            let Some(match_index) = match_index else {
                return Err(LiveError::GroupFill(format!(
                    "leg {} requires one exact symbol/action/quantity fill; partial group fills are prohibited",
                    leg.index
                )));
            };
            ordered.push(unmatched.remove(match_index));
        }
        Ok(ordered)
    }

    fn filled_snapshot(&self, fills: &[Fill]) -> LiveResult<GroupSnapshot> {
        let mut net_price = ZERO;
        let mut gross_cash = ZERO;
        let mut fees = ZERO;
        let mut filled_legs = Vec::with_capacity(fills.len());
        for (current, fill) in self.snapshot.legs.iter().zip(fills.iter()) {
            let notional = fill.quantity * fill.price;
            let (leg_gross_cash, position_quantity) = if current.action() == Side::Buy {
                net_price += fill.price * current.ratio();
                (-notional, fill.quantity)
            } else {
                net_price -= fill.price * current.ratio();
                (notional, -fill.quantity)
            };
            let leg_net_cash = leg_gross_cash - fill.fee;
            gross_cash += leg_gross_cash;
            fees += fill.fee;
            let mut leg = current.clone();
            leg.status = GroupStatus::Filled;
            leg.filled_quantity = fill.quantity;
            leg.remaining_quantity = ZERO;
            leg.filled_notional = notional;
            leg.average_fill_price = Some(fill.price);
            leg.fee = fill.fee;
            leg.gross_cash = leg_gross_cash;
            leg.net_cash = leg_net_cash;
            leg.position_quantity = position_quantity;
            leg.fill = Some(fill.clone());
            filled_legs.push(leg);
        }

        self.validate_net_limit(net_price)?;
        Ok(GroupSnapshot {
            order: self.snapshot.order.clone(),
            order_type: self.snapshot.order_type,
            status: GroupStatus::Filled,
            legs: filled_legs,
            net_price: Some(net_price),
            gross_cash,
            fees,
            net_cash: gross_cash - fees,
            transition_count: self.snapshot.transition_count + 1,
            reason: None,
        })
    }

    fn validate_net_limit(&self, net_price: Decimal18) -> LiveResult<()> {
        let order_type = self.snapshot.order_type;
        let limit = self.snapshot.order.limit_price;
        match order_type {
            GroupOrderType::Market => Ok(()),
            GroupOrderType::NetDebit => {
                let Some(limit) = limit else {
                    return Err(LiveError::GroupFill(
                        "net-debit group requires a limit_price".into(),
                    ));
                };
                if net_price <= ZERO {
                    return Err(LiveError::GroupFill(
                        "net-debit group must execute for a debit".into(),
                    ));
                }
                if net_price > limit {
                    return Err(LiveError::GroupFill(
                        "net-debit fill exceeds the group limit_price".into(),
                    ));
                }
                Ok(())
            }
            GroupOrderType::NetCredit => {
                let Some(limit) = limit else {
                    return Err(LiveError::GroupFill(
                        "net-credit group requires a limit_price".into(),
                    ));
                };
                if net_price >= ZERO {
                    return Err(LiveError::GroupFill(
                        "net-credit group must execute for a credit".into(),
                    ));
                }
                if -net_price < limit {
                    return Err(LiveError::GroupFill(
                        "net-credit fill is below the group limit_price".into(),
                    ));
                }
                Ok(())
            }
        }
    }

    fn require_active(&self, operation: &str) -> LiveResult<()> {
        if !self.snapshot.is_active() {
            return Err(LiveError::GroupTransition(format!(
                "cannot {operation} group in {} state",
                status_name(self.snapshot.status)
            )));
        }
        Ok(())
    }

    fn require_status(&self, operation: &str, statuses: &[GroupStatus]) -> LiveResult<()> {
        if !statuses.contains(&self.snapshot.status) {
            return Err(LiveError::GroupTransition(format!(
                "cannot {operation} group in {} state",
                status_name(self.snapshot.status)
            )));
        }
        Ok(())
    }

    fn normalize_reason(reason: Option<String>) -> LiveResult<Option<String>> {
        match reason {
            None => Ok(None),
            Some(reason) => {
                let normalized = reason.trim();
                if normalized.is_empty() {
                    return Err(LiveError::Validation("reason must not be empty".into()));
                }
                Ok(Some(normalized.to_string()))
            }
        }
    }

    fn resolve_order_type(
        order: &MultiLegOrder,
        order_type: Option<GroupOrderType>,
    ) -> LiveResult<GroupOrderType> {
        match order_type {
            None => {
                if order.order_type == OrderType::Market {
                    Ok(GroupOrderType::Market)
                } else {
                    Err(LiveError::GroupTransition(
                        "limit multi-leg order requires NET_DEBIT or NET_CREDIT order_type".into(),
                    ))
                }
            }
            Some(resolved) => {
                if order.order_type == OrderType::Market {
                    if resolved != GroupOrderType::Market {
                        return Err(LiveError::GroupTransition(
                            "market multi-leg order requires MARKET order_type".into(),
                        ));
                    }
                } else if resolved == GroupOrderType::Market {
                    return Err(LiveError::GroupTransition(
                        "limit multi-leg order requires a net price order_type".into(),
                    ));
                }
                Ok(resolved)
            }
        }
    }
}

/// Registry and deterministic operation router for atomic order groups.
#[derive(Clone, Debug, Default)]
pub struct MultiLegOms {
    groups: HashMap<String, MultiLegOrderGroup>,
    order: Vec<String>,
    used_group_ids: HashSet<String>,
    used_fill_ids: HashSet<String>,
}

impl MultiLegOms {
    /// Construct an empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a validated local group in `NEW` state.
    pub fn submit(
        &mut self,
        order: MultiLegOrder,
        order_type: Option<GroupOrderType>,
    ) -> LiveResult<GroupSnapshot> {
        if self.used_group_ids.contains(&order.order_id) {
            return Err(LiveError::DuplicateGroup(format!(
                "group order_id {:?} was already used",
                order.order_id
            )));
        }
        let group_id = order.order_id.clone();
        let group = MultiLegOrderGroup::new(order, order_type)?;
        let snapshot = group.snapshot().clone();
        self.groups.insert(group_id.clone(), group);
        self.used_group_ids.insert(group_id.clone());
        self.order.push(group_id);
        Ok(snapshot)
    }

    /// Acknowledge a locally new group.
    pub fn accept(&mut self, group_id: &str) -> LiveResult<GroupSnapshot> {
        self.group_mut(group_id)?.accept()
    }

    /// Begin one atomic matching evaluation.
    pub fn begin_evaluation(&mut self, group_id: &str) -> LiveResult<GroupSnapshot> {
        self.group_mut(group_id)?.begin_evaluation()
    }

    /// End an evaluation that found no complete fill.
    pub fn end_evaluation(&mut self, group_id: &str) -> LiveResult<GroupSnapshot> {
        self.group_mut(group_id)?.end_evaluation()
    }

    /// Atomically commit a complete fill batch to an evaluating group.
    pub fn fill(&mut self, group_id: &str, fills: Vec<Fill>) -> LiveResult<GroupSnapshot> {
        let mut duplicate_ids: Vec<&str> = fills
            .iter()
            .filter(|fill| self.used_fill_ids.contains(&fill.fill_id))
            .map(|fill| fill.fill_id.as_str())
            .collect();
        if !duplicate_ids.is_empty() {
            duplicate_ids.sort_unstable();
            return Err(LiveError::GroupFill(format!(
                "fill_id {:?} was already used",
                duplicate_ids[0]
            )));
        }
        let snapshot = self.group_mut(group_id)?.fill(fills)?;
        for fill in snapshot.fills() {
            self.used_fill_ids.insert(fill.fill_id);
        }
        Ok(snapshot)
    }

    /// Cancel a group, including one currently under evaluation.
    pub fn cancel(&mut self, group_id: &str, reason: Option<String>) -> LiveResult<GroupSnapshot> {
        self.group_mut(group_id)?.cancel(reason)
    }

    /// Reject an active group.
    pub fn reject(&mut self, group_id: &str, reason: Option<String>) -> LiveResult<GroupSnapshot> {
        self.group_mut(group_id)?.reject(reason)
    }

    /// Expire a working group.
    pub fn expire(&mut self, group_id: &str, reason: Option<String>) -> LiveResult<GroupSnapshot> {
        self.group_mut(group_id)?.expire(reason)
    }

    /// Return the current immutable state for a group.
    pub fn state(&self, group_id: &str) -> LiveResult<GroupSnapshot> {
        Ok(self.group(group_id)?.snapshot().clone())
    }

    /// Return committed fills in original leg order.
    pub fn fills(&self, group_id: &str) -> LiveResult<Vec<Fill>> {
        Ok(self.group(group_id)?.fills().to_vec())
    }

    /// Return active groups in deterministic submission order.
    pub fn active_states(&self) -> Vec<GroupSnapshot> {
        self.order
            .iter()
            .filter_map(|id| self.groups.get(id))
            .filter(|group| group.snapshot().is_active())
            .map(|group| group.snapshot().clone())
            .collect()
    }

    fn group(&self, group_id: &str) -> LiveResult<&MultiLegOrderGroup> {
        self.groups
            .get(group_id)
            .ok_or_else(|| LiveError::UnknownGroup(group_id.to_string()))
    }

    fn group_mut(&mut self, group_id: &str) -> LiveResult<&mut MultiLegOrderGroup> {
        self.groups
            .get_mut(group_id)
            .ok_or_else(|| LiveError::UnknownGroup(group_id.to_string()))
    }
}

fn status_name(status: GroupStatus) -> &'static str {
    match status {
        GroupStatus::New => "new",
        GroupStatus::Accepted => "accepted",
        GroupStatus::Evaluating => "evaluating",
        GroupStatus::Filled => "filled",
        GroupStatus::Canceled => "canceled",
        GroupStatus::Rejected => "rejected",
        GroupStatus::Expired => "expired",
    }
}

#[cfg(test)]
mod tests {
    use num::Decimal18;

    use super::{GroupOrderType, GroupStatus, MultiLegOms, MultiLegOrderGroup};
    use crate::accounting::Fill;
    use crate::orders::{MultiLegOrder, OrderLeg, OrderType, Side};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn leg(symbol: &str, side: Side, quantity: &str) -> OrderLeg {
        OrderLeg::new(
            Symbol::new(symbol).unwrap(),
            side,
            dec(quantity),
            dec("1"),
            None,
        )
        .unwrap()
    }

    fn call_spread(order_id: &str, order_type: OrderType, limit: Option<&str>) -> MultiLegOrder {
        MultiLegOrder::new(
            order_id,
            vec![leg("AAPL", Side::Buy, "1"), leg("MSFT", Side::Sell, "1")],
            order_type,
            crate::orders::TimeInForce::Day,
            None,
            limit.map(dec),
        )
        .unwrap()
    }

    fn fill(
        order_id: &str,
        fill_id: &str,
        symbol: &str,
        side: Side,
        quantity: &str,
        price: &str,
    ) -> Fill {
        Fill::new(
            fill_id,
            order_id,
            Symbol::new(symbol).unwrap(),
            side,
            dec(quantity),
            dec(price),
            Decimal18::ZERO,
            Some("taker".into()),
        )
        .unwrap()
    }

    #[test]
    fn group_fill_is_atomic_and_net_price_is_ratio_weighted() {
        let order = call_spread("g1", OrderType::Market, None);
        let mut group = MultiLegOrderGroup::new(order, None).unwrap();
        group.accept().unwrap();
        group.begin_evaluation().unwrap();
        let snapshot = group
            .fill(vec![
                fill("g1", "f1", "AAPL", Side::Buy, "1", "100"),
                fill("g1", "f2", "MSFT", Side::Sell, "1", "101"),
            ])
            .unwrap();
        assert_eq!(snapshot.status, GroupStatus::Filled);
        assert_eq!(snapshot.net_price, Some(dec("-1")));
        assert_eq!(snapshot.net_credit(), dec("1"));
        assert_eq!(snapshot.net_debit(), Decimal18::ZERO);
        assert_eq!(snapshot.gross_cash, dec("1"));
    }

    #[test]
    fn partial_group_fill_is_rejected() {
        let order = call_spread("g2", OrderType::Market, None);
        let mut group = MultiLegOrderGroup::new(order, None).unwrap();
        group.accept().unwrap();
        group.begin_evaluation().unwrap();
        let result = group.fill(vec![fill("g2", "f1", "AAPL", Side::Buy, "1", "100")]);
        assert!(result.is_err());
        assert_eq!(group.snapshot().status, GroupStatus::Evaluating);
    }

    #[test]
    fn net_debit_limit_is_enforced() {
        let order = call_spread("g3", OrderType::Limit, Some("1"));
        let mut group = MultiLegOrderGroup::new(order, Some(GroupOrderType::NetDebit)).unwrap();
        group.accept().unwrap();
        group.begin_evaluation().unwrap();
        let result = group.fill(vec![
            fill("g3", "f1", "AAPL", Side::Buy, "1", "100"),
            fill("g3", "f2", "MSFT", Side::Sell, "1", "101"),
        ]);
        assert!(result.is_err());
    }

    #[test]
    fn oms_rejects_duplicate_fill_ids() {
        let mut oms = MultiLegOms::new();
        oms.submit(call_spread("g4", OrderType::Market, None), None)
            .unwrap();
        oms.accept("g4").unwrap();
        oms.begin_evaluation("g4").unwrap();
        oms.fill(
            "g4",
            vec![
                fill("g4", "f1", "AAPL", Side::Buy, "1", "100"),
                fill("g4", "f2", "MSFT", Side::Sell, "1", "101"),
            ],
        )
        .unwrap();
        oms.submit(call_spread("g5", OrderType::Market, None), None)
            .unwrap();
        oms.accept("g5").unwrap();
        oms.begin_evaluation("g5").unwrap();
        let result = oms.fill(
            "g5",
            vec![
                fill("g5", "f1", "AAPL", Side::Buy, "1", "100"),
                fill("g5", "f3", "MSFT", Side::Sell, "1", "101"),
            ],
        );
        assert!(result.is_err());
    }

    #[test]
    fn unknown_group_is_reported() {
        let oms = MultiLegOms::new();
        assert!(oms.state("missing").is_err());
    }
}
