//! Owns resting-order lifecycle and event scheduling using shared matching policy.
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use num::Decimal;
use std::cmp::Ordering;
use std::cmp::Reverse;
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::collections::BinaryHeap;
use std::{cell::OnceCell, sync::Arc};

use super::event_bus::EngineEvent;
use super::event_bus::EventBus;

/// One accepted venue-side working order.
#[derive(Clone, Debug)]
struct WorkingOrder {
    order: EngineOrder,
    status: String,
    filled_quantity: Decimal,
    response_pending: bool,
    stop_triggered: bool,
    booked_quantity: Decimal,
    expires_at_ns: Option<i64>,
    expiry_waiting_for_fill: bool,
    reason: Option<String>,
}

impl WorkingOrder {
    fn execution_order(&self) -> ExecutionOrder {
        ExecutionOrder {
            order_id: self.order.order_id.clone(),
            symbol: self.order.symbol.clone(),
            side: self.order.side.clone(),
            quantity: self.order.quantity,
            filled_quantity: self.booked_quantity,
            remaining_quantity: self
                .order
                .quantity
                .checked_sub(self.booked_quantity)
                .expect("bounded fills"),
            status: if self.filled_quantity > self.booked_quantity {
                "pending_fill".into()
            } else {
                self.status.clone()
            },
            reason: self.reason.clone(),
        }
    }
    fn remaining(&self) -> Result<Decimal, BacktestError> {
        self.order
            .quantity
            .checked_sub(self.filled_quantity)
            .map_err(|error| {
                BacktestError::Arithmetic(format!(
                    "order {} remaining quantity exceeds the decimal128(38,18) bound: {error}",
                    self.order.order_id
                ))
            })
    }

    fn is_active(&self) -> bool {
        !matches!(
            self.status.as_str(),
            "filled" | "canceled" | "rejected" | "expired" | "replaced"
        )
    }
}

/// The venue retains only prices and liquidity, not copied feed identifiers.
/// Execution responses derive their own identities and use the observed clock.
#[derive(Clone, Copy, Debug)]
struct MarketSnapshot {
    prices: [Decimal; 2],
    quantities: [Decimal; 2],
    quote: bool,
}
impl MarketSnapshot {
    fn from_event(market: &EngineMarket) -> Option<Self> {
        match market.view() {
            harness_trading::market::MarketView::Quote {
                bid,
                ask,
                bid_quantity,
                ask_quantity,
                ..
            } => Some(Self {
                prices: [bid, ask],
                quantities: [bid_quantity, ask_quantity],
                quote: true,
            }),
            harness_trading::market::MarketView::Trade {
                price, quantity, ..
            } => Some(Self {
                prices: [price; 2],
                quantities: [quantity; 2],
                quote: false,
            }),
            harness_trading::market::MarketView::Bar(_) => None,
        }
    }
    fn quantity(self, side: &str) -> Decimal {
        self.quantities[usize::from(side == "buy")]
    }
}

/// Synchronous deterministic resting-order venue with `simple_touch` fills.
#[derive(Debug)]
pub struct SimulatedVenue {
    head_ns: Option<i64>,
    sequence: i64,
    ordinal: u64,
    orders: Vec<WorkingOrder>,
    latest_markets: BTreeMap<String, MarketSnapshot>,
    active: BTreeMap<String, Vec<usize>>,
    order_indices: BTreeMap<String, usize>,
    expirations: BinaryHeap<Reverse<(i64, usize)>>,
    pending_fills: BTreeMap<String, BTreeSet<usize>>,
    order_view: OnceCell<Arc<Vec<ExecutionOrder>>>,
}

impl Default for SimulatedVenue {
    fn default() -> Self {
        Self {
            head_ns: None,
            sequence: -1,
            ordinal: 0,
            orders: Vec::new(),
            latest_markets: BTreeMap::new(),
            active: BTreeMap::new(),
            order_indices: BTreeMap::new(),
            expirations: BinaryHeap::new(),
            pending_fills: BTreeMap::new(),
            order_view: OnceCell::new(),
        }
    }
}

impl SimulatedVenue {
    /// Create an empty venue.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn observe(&mut self, timestamp_ns: i64, sequence: u64) {
        if self.head_ns.is_none_or(|head| timestamp_ns > head) {
            self.head_ns = Some(timestamp_ns);
        }
        if i64::try_from(sequence).unwrap_or(i64::MAX) > self.sequence {
            self.sequence = i64::try_from(sequence).unwrap_or(i64::MAX);
        }
    }

    fn next_sequence(&mut self) -> u64 {
        self.sequence += 1;
        u64::try_from(self.sequence).unwrap_or(0)
    }

    fn next_id(&mut self, kind: &str) -> String {
        self.ordinal += 1;
        format!("sim-{kind}-{}", self.ordinal)
    }

    const fn response_timestamp(&self, clock_ns: i64) -> i64 {
        match self.head_ns {
            Some(head) => {
                if clock_ns > head {
                    clock_ns
                } else {
                    head
                }
            }
            None => clock_ns,
        }
    }

    pub(super) fn on_market(
        &mut self,
        market: &EngineMarket,
        clock_ns: i64,
        bus: &mut EventBus,
    ) -> Result<(), BacktestError> {
        // The venue trades book and print events only; a bar is explicitly not
        // a matchable event and never enters the last-trade book.
        let Some(snapshot) = MarketSnapshot::from_event(market) else {
            return Ok(());
        };
        self.observe(market.timestamp_ns(), market.sequence());
        if let Some(latest) = self.latest_markets.get_mut(market.symbol()) {
            *latest = snapshot;
        } else {
            self.latest_markets.insert(market.symbol().into(), snapshot);
        }
        // Retain submission order while scanning only this symbol's live orders.
        // Taking/restoring the vector reuses its allocation, including on errors.
        let Some(indices) = self.active.get_mut(market.symbol()) else {
            return Ok(());
        };
        if indices.is_empty() {
            return Ok(());
        }
        let mut indices = std::mem::take(indices);
        let result = (|| {
            for &index in &indices {
                if self.orders[index].response_pending || !self.orders[index].is_active() {
                    continue;
                }
                let available = snapshot.quantity(&self.orders[index].order.side);
                self.attempt(index, snapshot, available, clock_ns, bus)?;
            }
            Ok(())
        })();
        indices.retain(|&index| self.orders[index].is_active());
        *self
            .active
            .get_mut(market.symbol())
            .expect("symbol index retained") = indices;
        result?;
        Ok(())
    }

    pub(super) fn on_report(&mut self, report: &EngineReport) {
        self.observe(report.timestamp_ns, report.sequence);
        if report.execution_type == "acknowledged" {
            return;
        }
        if let Some(&index) = self.order_indices.get(&report.order_id) {
            self.orders[index].response_pending = false;
        }
    }

    pub(super) fn on_fill(&mut self, fill: &EngineFill) {
        self.order_view.take();
        self.observe(fill.timestamp_ns, fill.sequence);
        if let Some(&index) = self.order_indices.get(&fill.order_id) {
            let working = &mut self.orders[index];
            working.booked_quantity = working
                .booked_quantity
                .checked_add(fill.quantity)
                .expect("scheduled fill quantity was already checked");
            if working.booked_quantity == working.filled_quantity {
                if let Some(pending) = self.pending_fills.get_mut(&working.order.symbol) {
                    pending.remove(&index);
                }
            }
            if working.expiry_waiting_for_fill && working.booked_quantity == working.filled_quantity
            {
                working.expiry_waiting_for_fill = false;
                self.expirations.push(Reverse((
                    working.expires_at_ns.expect("deferred day expiration"),
                    index,
                )));
            }
        }
    }

    pub(super) fn orders(&self) -> Arc<Vec<ExecutionOrder>> {
        self.order_view
            .get_or_init(|| {
                Arc::new(
                    self.orders
                        .iter()
                        .map(WorkingOrder::execution_order)
                        .collect(),
                )
            })
            .clone()
    }

    /// A scheduled fill cannot be unbooked by a cancellation request.
    pub(super) fn cancel(&mut self, order_id: &str) -> bool {
        let Some(&index) = self.order_indices.get(order_id) else {
            return true;
        };
        let working = &mut self.orders[index];
        if working.filled_quantity > working.booked_quantity {
            return false;
        }
        if working.is_active() {
            working.status = "canceled".into();
            self.order_view.take();
        }
        true
    }

    pub(super) fn expire(&mut self, timestamp_ns: i64) {
        let mut expired = false;
        while self
            .expirations
            .peek()
            .is_some_and(|Reverse((end, _))| *end <= timestamp_ns)
        {
            let Reverse((_, index)) = self.expirations.pop().expect("due expiration");
            let working = &mut self.orders[index];
            if !working.is_active() {
                continue;
            }
            if working.filled_quantity != working.booked_quantity {
                // The scheduled fill is irrevocable. Re-arm this deadline only
                // when that fill is booked, never by scanning historical orders.
                working.expiry_waiting_for_fill = true;
                continue;
            }
            working.status = "expired".into();
            self.order_view.take();
            expired = true;
        }
        if expired {
            // Retire a simultaneous deadline in one pass, rather than filtering
            // the same live index once for every expiring order.
            for active in self.active.values_mut() {
                active.retain(|index| self.orders[*index].is_active());
            }
        }
    }

    pub(super) fn order_symbol(&self, order_id: &str) -> Option<&str> {
        self.order_indices
            .get(order_id)
            .map(|index| self.orders[*index].order.symbol.as_str())
    }

    pub(super) fn working_orders(&self, symbol: &str) -> Vec<ExecutionOrder> {
        let mut orders: Vec<_> = self
            .active
            .get(symbol)
            .into_iter()
            .flatten()
            .filter_map(|index| {
                let working = &self.orders[*index];
                working.is_active().then(|| working.execution_order())
            })
            .collect();
        orders.extend(
            self.pending_fills
                .get(symbol)
                .into_iter()
                .flatten()
                .filter_map(|index| {
                    let working = &self.orders[*index];
                    (!working.is_active()).then(|| working.execution_order())
                }),
        );
        orders
    }

    pub(super) fn reject(&mut self, order: EngineOrder, reason: String) {
        self.order_view.take();
        let index = self.orders.len();
        self.order_indices.insert(order.order_id.clone(), index);
        self.orders.push(WorkingOrder {
            order,
            status: "rejected".into(),
            filled_quantity: Decimal::ZERO,
            response_pending: false,
            stop_triggered: false,
            booked_quantity: Decimal::ZERO,
            expires_at_ns: None,
            expiry_waiting_for_fill: false,
            reason: Some(reason),
        });
    }

    pub(super) fn submit(
        &mut self,
        order: EngineOrder,
        expires_at_ns: Option<i64>,
        clock_ns: i64,
        bus: &mut EventBus,
    ) -> Result<(), BacktestError> {
        self.order_view.take();
        self.observe(order.timestamp_ns, order.sequence);
        let working = WorkingOrder {
            order: order.clone(),
            status: "accepted".to_string(),
            filled_quantity: Decimal::ZERO,
            response_pending: false,
            stop_triggered: false,
            booked_quantity: Decimal::ZERO,
            expires_at_ns,
            expiry_waiting_for_fill: false,
            reason: None,
        };
        let index = self.orders.len();
        if let Some(end) = expires_at_ns {
            self.expirations.push(Reverse((end, index)));
        }
        self.order_indices
            .entry(order.order_id.clone())
            .or_insert(index);
        self.active
            .entry(order.symbol.clone())
            .or_default()
            .push(index);
        self.orders.push(working);
        self.schedule_report(&order, "acknowledged", "accepted", clock_ns, bus);
        if let Some(market) = self.latest_markets.get(&order.symbol).copied() {
            let available = market.quantity(&order.side);
            let index = self.order_indices[&order.order_id];
            self.attempt(index, market, available, clock_ns, bus)?;
            if !self.orders[index].is_active() {
                self.active
                    .get_mut(&order.symbol)
                    .expect("submitted symbol")
                    .retain(|&candidate| candidate != index);
            }
        }
        if order.time_in_force == "ioc" {
            // Preserve an already scheduled partial fill, cancel only its remainder.
            let index = self.order_indices[&order.order_id];
            if self.orders[index].is_active() {
                self.orders[index].status = "canceled".into();
            }
        }
        Ok(())
    }

    fn attempt(
        &mut self,
        index: usize,
        market: MarketSnapshot,
        available: Decimal,
        clock_ns: i64,
        bus: &mut EventBus,
    ) -> Result<(), BacktestError> {
        let (order, remaining) = {
            let working = &self.orders[index];
            (working.order.clone(), working.remaining()?)
        };
        if remaining.cmp_value(Decimal::ZERO) != Ordering::Greater {
            return Ok(());
        }
        if available <= Decimal::ZERO {
            return Ok(());
        }
        let Some(decision) =
            simple_touch_price(&order, market, &mut self.orders[index].stop_triggered)
        else {
            return Ok(());
        };
        let quantity = if remaining.cmp_value(available) == Ordering::Less {
            remaining
        } else {
            available
        };
        if quantity.cmp_value(Decimal::ZERO) != Ordering::Greater {
            return Ok(());
        }
        self.schedule_fill(index, quantity, decision, clock_ns, bus)
    }

    fn schedule_fill(
        &mut self,
        index: usize,
        quantity: Decimal,
        price: Decimal,
        clock_ns: i64,
        bus: &mut EventBus,
    ) -> Result<(), BacktestError> {
        self.order_view.take();
        {
            let working = &mut self.orders[index];
            working.filled_quantity =
                working
                    .filled_quantity
                    .checked_add(quantity)
                    .map_err(|error| {
                        BacktestError::Arithmetic(format!(
                            "order {} filled quantity exceeds the decimal128(38,18) bound: {error}",
                            working.order.order_id
                        ))
                    })?;
            working.status = if working.remaining()?.is_zero() {
                "filled".to_string()
            } else {
                "partially_filled".to_string()
            };
            working.response_pending = true;
        }
        let order = self.orders[index].order.clone();
        self.pending_fills
            .entry(order.symbol.clone())
            .or_default()
            .insert(index);
        self.schedule_report(&order, "fill", "filled", clock_ns, bus);
        let timestamp_ns = self.response_timestamp(clock_ns);
        let fill_id = self.next_id("fill");
        let sequence = self.next_sequence();
        bus.schedule(EngineEvent::Fill(EngineFill {
            event_id: fill_id.clone(),
            timestamp_ns,
            sequence,
            fill_id,
            order_id: order.order_id.clone(),
            symbol: order.symbol.clone(),
            side: order.side.clone(),
            quantity,
            price,
            fee: Decimal::ZERO,
            liquidity: "taker".to_string(),
        }));
        Ok(())
    }

    fn schedule_report(
        &mut self,
        order: &EngineOrder,
        execution_type: &str,
        _status: &str,
        clock_ns: i64,
        bus: &mut EventBus,
    ) {
        let timestamp_ns = self.response_timestamp(clock_ns);
        let report_id = self.next_id("report");
        let sequence = self.next_sequence();
        bus.schedule(EngineEvent::Report(EngineReport {
            event_id: report_id,
            timestamp_ns,
            sequence,
            order_id: order.order_id.clone(),
            execution_type: execution_type.to_string(),
        }));
    }
}

fn simple_touch_price(
    order: &EngineOrder,
    market: MarketSnapshot,
    activated: &mut bool,
) -> Option<Decimal> {
    use harness_trading::orders::{OrderType, Side};
    let side = match order.side.as_str() {
        "buy" => Side::Buy,
        "sell" => Side::Sell,
        _ => return None,
    };
    let kind = match order.order_type.as_str() {
        "market" => OrderType::Market,
        "limit" => OrderType::Limit,
        "stop" => OrderType::Stop,
        "stop_limit" => OrderType::StopLimit,
        _ => return None,
    };
    let price = market.prices[usize::from(side == Side::Buy)];
    let quote = market.quote;
    let (triggered, price) = harness_trading::matching::touch(
        side,
        kind,
        order.limit_price,
        order.stop_price,
        price,
        quote,
        *activated,
    );
    *activated = triggered;
    price
}
