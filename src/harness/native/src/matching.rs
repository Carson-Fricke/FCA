//! Synchronous deterministic resting-order venue for top-of-book replay,
//! ported from `src/harness/venues/sim.py` (the equities single-leg path).
//!
//! Commands return no outcomes. Acknowledgements, fills, cancellations,
//! expiries, and replacement reports are appended to the venue's ordered
//! record logs and queued with explicit timestamps; [`SimulatedVenue::drain`]
//! dispatches them deterministically. The venue is self-contained: it keeps its
//! own response queue and an optional shared [`SimClock`], so no event-bus
//! envelope lifetime is required.

use crate::decimal::{self, DecimalError};
use crate::fills::{FillModel, Liquidity};
use crate::market::{MarketEvent, MarketOrder, OrderType, Side, TimeInForce};
use crate::market_bus::SharedClock;
use crate::oms::{ExecutionType, OrderStatus};
use crate::simple_touch::{DecideOptions, FillDecision};

/// A checked failure from the simulated venue.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Allocation failure.
    OutOfMemory,
    /// A required argument was invalid.
    InvalidArgument,
    /// The symbol was invalid.
    InvalidSymbol,
    /// A decimal operation failed.
    InvalidDecimal,
    /// A value was out of range.
    OutOfRange,
    /// Command refers to an unknown simulated order.
    UnknownOrder,
    /// Order identifier submitted more than once.
    DuplicateOrder,
    /// Command on an inactive order or a pending-response race.
    VenueState,
    /// Fill decision inconsistent with the working order.
    DecisionLedger,
    /// A timestamped order or market event is required before responses.
    NoTimestamp,
}

impl core::fmt::Display for Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfMemory => "out of memory",
            Self::InvalidArgument => "invalid argument",
            Self::InvalidSymbol => "invalid symbol",
            Self::InvalidDecimal => "invalid decimal",
            Self::OutOfRange => "out of range",
            Self::UnknownOrder => "unknown order",
            Self::DuplicateOrder => "duplicate order",
            Self::VenueState => "venue state error",
            Self::DecisionLedger => "decision ledger inconsistency",
            Self::NoTimestamp => "no timestamp observed",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for Error {}

fn decimal_error(error: DecimalError) -> Error {
    match error {
        DecimalError::OutOfRange => Error::OutOfRange,
        DecimalError::InvalidDecimal => Error::InvalidDecimal,
    }
}

fn market_error(_error: crate::market::Error) -> Error {
    Error::InvalidArgument
}

/// Immutable inspection snapshot of one venue-side order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SimOrderState {
    /// The submitted order.
    pub order: MarketOrder<'static>,
    /// Current venue-side status.
    pub status: OrderStatus,
    /// Cumulative filled quantity.
    pub filled_quantity: i128,
    /// Remaining quantity.
    pub remaining_quantity: i128,
    /// Whether a stop order is still awaiting its trigger.
    pub armed: bool,
    /// The snapshot that triggered a stop order.
    pub triggering_market: Option<MarketEvent<'static>>,
    /// The decision price.
    pub decision_price: Option<i128>,
    /// Most recent fill decision.
    pub last_decision: Option<FillDecision<'static>>,
}

impl SimOrderState {
    /// The order identifier.
    pub fn order_id(&self) -> &str {
        self.order.order_id
    }

    /// Whether the venue can still match or cancel the order.
    pub fn is_active(&self) -> bool {
        !crate::oms::is_terminal_status(self.status)
    }

    /// Whether a stop order has crossed its trigger.
    pub fn triggered(&self) -> bool {
        if matches!(
            self.order.order_type,
            OrderType::Stop | OrderType::StopLimit
        ) {
            return !self.armed;
        }
        true
    }
}

/// One scheduled execution report record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReportRecord {
    /// Envelope event identifier.
    pub event_id: String,
    /// Report identifier.
    pub report_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
    /// Reported status.
    pub status: OrderStatus,
    /// Reported execution type.
    pub execution_type: ExecutionType,
    /// Total order quantity.
    pub order_quantity: i128,
    /// Cumulative filled quantity.
    pub filled_quantity: i128,
    /// Remaining quantity.
    pub remaining_quantity: i128,
    /// Average fill price.
    pub average_fill_price: Option<i128>,
    /// Last fill quantity.
    pub last_fill_quantity: i128,
    /// Last fill price.
    pub last_fill_price: Option<i128>,
}

/// One scheduled fill record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillRecord {
    /// Envelope event identifier.
    pub event_id: String,
    /// Fill identifier.
    pub fill_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Canonical symbol.
    pub symbol: String,
    /// Fill side.
    pub side: Side,
    /// Filled quantity.
    pub quantity: i128,
    /// Fill price.
    pub price: i128,
    /// Liquidity flag.
    pub liquidity: Liquidity,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
}

#[derive(Clone, Debug)]
struct WorkingOrder {
    order: MarketOrder<'static>,
    status: OrderStatus,
    filled_quantity: i128,
    filled_notional: i128,
    armed: bool,
    stop_triggered: bool,
    triggering_market: Option<MarketEvent<'static>>,
    decision_price: Option<i128>,
    last_decision: Option<FillDecision<'static>>,
    response_pending: bool,
}

impl WorkingOrder {
    fn remaining_quantity(&self) -> i128 {
        self.order.quantity - self.filled_quantity
    }

    fn is_active(&self) -> bool {
        !crate::oms::is_terminal_status(self.status)
    }
}

#[derive(Clone, Debug)]
struct ScheduledEnvelope {
    timestamp_ns: i128,
    sequence: u64,
    class_priority: u8,
    feed_ordinal: u64,
    event_id: String,
    order_id: String,
    execution_type: Option<ExecutionType>,
    is_fill: bool,
}

/// Match single-leg orders against explicit quote/trade events. The default
/// fill model is `SimpleTouch`.
#[derive(Clone, Debug)]
pub struct SimulatedVenue {
    fill_model: FillModel,
    latency_ns: i128,
    venue_name: String,
    sim_clock: Option<SharedClock>,
    orders: Vec<WorkingOrder>,
    used_order_ids: Vec<String>,
    sequence_head: i128,
    ordinal: u64,
    head_ns: Option<i128>,
    latest_market: Option<MarketEvent<'static>>,
    latest_markets: Vec<(String, MarketEvent<'static>)>,
    decisions: Vec<FillDecision<'static>>,
    report_records: Vec<ReportRecord>,
    fill_records: Vec<FillRecord>,
    response_queue: Vec<ScheduledEnvelope>,
    dispatched: Vec<String>,
    feed_ordinal: u64,
}

impl SimulatedVenue {
    /// Construct a venue with the given fill model and latency.
    pub fn new(
        fill_model: FillModel,
        latency_ns: i128,
        sim_clock: Option<SharedClock>,
    ) -> Result<Self, Error> {
        if latency_ns < 0 {
            return Err(Error::InvalidArgument);
        }
        Ok(Self {
            fill_model,
            latency_ns,
            venue_name: "sim".to_owned(),
            sim_clock,
            orders: Vec::new(),
            used_order_ids: Vec::new(),
            sequence_head: -1,
            ordinal: 0,
            head_ns: None,
            latest_market: None,
            latest_markets: Vec::new(),
            decisions: Vec::new(),
            report_records: Vec::new(),
            fill_records: Vec::new(),
            response_queue: Vec::new(),
            dispatched: Vec::new(),
            feed_ordinal: 0,
        })
    }

    /// All fill decisions in deterministic matching order.
    pub fn decisions(&self) -> &[FillDecision<'static>] {
        &self.decisions
    }

    /// All scheduled report records.
    pub fn report_records(&self) -> &[ReportRecord] {
        &self.report_records
    }

    /// All scheduled fill records.
    pub fn fill_records(&self) -> &[FillRecord] {
        &self.fill_records
    }

    /// Envelope identifiers dispatched so far.
    pub fn dispatched(&self) -> &[String] {
        &self.dispatched
    }

    /// The most recent market snapshot.
    pub fn latest_market(&self) -> Option<MarketEvent<'static>> {
        self.latest_market
    }

    /// Accept an order and schedule its acknowledgement and outcome.
    pub fn submit(&mut self, order: MarketOrder<'static>) -> Result<(), Error> {
        order.validate().map_err(market_error)?;
        if self.find_used_order_id(order.order_id).is_some() {
            return Err(Error::DuplicateOrder);
        }
        self.observe(order.timestamp_ns, order.sequence);
        let armed = matches!(order.order_type, OrderType::Stop | OrderType::StopLimit);
        self.orders.push(WorkingOrder {
            order,
            status: OrderStatus::Accepted,
            filled_quantity: 0,
            filled_notional: 0,
            armed,
            stop_triggered: false,
            triggering_market: None,
            decision_price: None,
            last_decision: None,
            response_pending: false,
        });
        self.used_order_ids.push(order.order_id.to_owned());
        let index = self.orders.len() - 1;
        self.schedule_report(
            index,
            ExecutionType::Acknowledged,
            OrderStatus::Accepted,
            0,
            None,
        )?;
        if let Some(latest) = self.latest_market_for(self.orders[index].order.symbol) {
            if !self.orders[index].armed {
                let decision = self.attempt(index, latest, None)?;
                if decision.is_none()
                    && self.orders[index].order.time_in_force == TimeInForce::Ioc
                    && self.is_matching_opportunity(index, latest)?
                {
                    self.schedule_expiry(index, false)?;
                }
            }
        }
        Ok(())
    }

    /// Schedule cancellation confirmation for an active non-pending order.
    pub fn cancel(&mut self, order_id: &str) -> Result<(), Error> {
        let index = self.working_index(order_id)?;
        if !self.orders[index].is_active() || self.orders[index].response_pending {
            return Err(Error::VenueState);
        }
        self.orders[index].status = OrderStatus::Canceled;
        self.orders[index].response_pending = true;
        self.schedule_report(
            index,
            ExecutionType::Canceled,
            OrderStatus::Canceled,
            0,
            None,
        )
    }

    /// Atomically report replacement and begin the replacement order.
    pub fn replace(
        &mut self,
        order_id: &str,
        replacement: MarketOrder<'static>,
    ) -> Result<(), Error> {
        replacement.validate().map_err(market_error)?;
        let index = self.working_index(order_id)?;
        if !self.orders[index].is_active() || self.orders[index].response_pending {
            return Err(Error::VenueState);
        }
        if self.find_used_order_id(replacement.order_id).is_some() {
            return Err(Error::DuplicateOrder);
        }
        if replacement.symbol != self.orders[index].order.symbol
            || replacement.side != self.orders[index].order.side
        {
            return Err(Error::VenueState);
        }

        self.orders[index].status = OrderStatus::Replaced;
        self.orders[index].response_pending = true;
        self.used_order_ids.push(replacement.order_id.to_owned());
        self.schedule_report(
            index,
            ExecutionType::Replaced,
            OrderStatus::Replaced,
            0,
            None,
        )?;

        let armed = matches!(
            replacement.order_type,
            OrderType::Stop | OrderType::StopLimit
        );
        self.orders.push(WorkingOrder {
            order: replacement,
            status: OrderStatus::Accepted,
            filled_quantity: 0,
            filled_notional: 0,
            armed,
            stop_triggered: false,
            triggering_market: None,
            decision_price: None,
            last_decision: None,
            response_pending: false,
        });
        let replacement_index = self.orders.len() - 1;
        self.schedule_report(
            replacement_index,
            ExecutionType::Acknowledged,
            OrderStatus::Accepted,
            0,
            None,
        )?;
        if let Some(latest) = self.latest_market_for(self.orders[replacement_index].order.symbol) {
            if !self.orders[replacement_index].armed {
                let decision = self.attempt(replacement_index, latest, None)?;
                if decision.is_none()
                    && self.orders[replacement_index].order.time_in_force == TimeInForce::Ioc
                    && self.is_matching_opportunity(replacement_index, latest)?
                {
                    self.schedule_expiry(replacement_index, false)?;
                }
            }
        }
        Ok(())
    }

    /// Validate the protocol command; positions belong to the accounting hub.
    pub fn request_position_snapshot(&self, account_id: &str) -> Result<(), Error> {
        if account_id.is_empty() {
            return Err(Error::InvalidArgument);
        }
        Ok(())
    }

    /// Return an immutable venue-side state snapshot.
    pub fn state(&self, order_id: &str) -> Result<SimOrderState, Error> {
        let index = self.working_index(order_id)?;
        Ok(self.snapshot(index))
    }

    /// Return all decisions made for one order.
    pub fn decisions_for(&self, order_id: &str) -> Result<usize, Error> {
        self.working_index(order_id)?;
        Ok(self
            .decisions
            .iter()
            .filter(|decision| decision.order_id == order_id)
            .count())
    }

    /// Route one market payload to the venue at its dispatch time.
    pub fn on_market(&mut self, event: MarketEvent<'static>) -> Result<(), Error> {
        event.validate().map_err(market_error)?;
        self.observe(event.timestamp_ns(), event.sequence());
        self.latest_market = Some(event);
        self.upsert_latest_market(event);
        let mut available_buy: i128 = match event {
            MarketEvent::Trade(trade) => trade.quantity,
            MarketEvent::Quote(quote) => quote.ask_quantity,
        };
        let mut available_sell: i128 = match event {
            MarketEvent::Trade(trade) => trade.quantity,
            MarketEvent::Quote(quote) => quote.bid_quantity,
        };
        for index in 0..self.orders.len() {
            if !self.orders[index].is_active() || self.orders[index].response_pending {
                continue;
            }
            if self.orders[index].order.symbol != event.symbol() {
                continue;
            }
            let available = if self.orders[index].order.side == Side::Buy {
                available_buy
            } else {
                available_sell
            };
            let decision = self.attempt(index, event, Some(available))?;
            match decision {
                None => {
                    if self.is_matching_opportunity(index, event)?
                        && self.orders[index].order.time_in_force == TimeInForce::Ioc
                    {
                        self.schedule_expiry(index, false)?;
                    }
                }
                Some(decision) => {
                    if self.orders[index].order.side == Side::Buy {
                        available_buy -= decision.quantity;
                    } else {
                        available_sell -= decision.quantity;
                    }
                }
            }
        }
        Ok(())
    }

    /// Dispatch all queued outcomes in deterministic order; returns the count.
    pub fn drain(&mut self) -> Result<usize, Error> {
        self.response_queue.sort_by(|a, b| {
            (a.timestamp_ns, a.sequence, a.class_priority, a.feed_ordinal).cmp(&(
                b.timestamp_ns,
                b.sequence,
                b.class_priority,
                b.feed_ordinal,
            ))
        });
        let envelopes: Vec<ScheduledEnvelope> = core::mem::take(&mut self.response_queue);
        let mut count = 0usize;
        for envelope in envelopes {
            if let Some(clock) = &self.sim_clock {
                let _ = clock.borrow_mut().advance_to(envelope.timestamp_ns);
            }
            if envelope.is_fill {
                self.observe(envelope.timestamp_ns, envelope.sequence);
            } else if envelope.execution_type != Some(ExecutionType::Acknowledged) {
                if let Ok(index) = self.working_index(&envelope.order_id) {
                    self.orders[index].response_pending = false;
                }
            }
            self.dispatched.push(envelope.event_id);
            count += 1;
        }
        Ok(count)
    }

    /// Return active order snapshots in submission order.
    pub fn active_orders(&self) -> Vec<SimOrderState> {
        (0..self.orders.len())
            .filter(|&index| self.orders[index].is_active())
            .map(|index| self.snapshot(index))
            .collect()
    }

    fn attempt(
        &mut self,
        index: usize,
        snap: MarketEvent<'static>,
        available_quantity: Option<i128>,
    ) -> Result<Option<FillDecision<'static>>, Error> {
        if self.orders[index].remaining_quantity() <= 0 {
            return Ok(None);
        }
        let order_type = self.orders[index].order.order_type;
        let is_stop = matches!(order_type, OrderType::Stop | OrderType::StopLimit);
        if is_stop && !self.orders[index].stop_triggered {
            if !self.is_triggered(&self.orders[index].order, snap)? {
                return Ok(None);
            }
            self.orders[index].stop_triggered = true;
            self.orders[index].armed = false;
            self.orders[index].triggering_market = Some(snap);
        }
        let remaining = self.orders[index].remaining_quantity();
        let stop_triggered = self.orders[index].stop_triggered;
        let opts = DecideOptions {
            remaining_quantity: Some(remaining),
            available_quantity,
            stop_triggered,
        };
        let mut decision = match self
            .fill_model
            .decide(&self.orders[index].order, &snap, &opts)
            .map_err(market_error)?
        {
            Some(decision) => decision,
            None => return Ok(None),
        };
        if is_stop {
            if let Some(triggering) = self.orders[index].triggering_market {
                if !same_market(decision.triggering_market, triggering) {
                    decision.triggering_market = triggering;
                }
            }
        }
        self.orders[index].triggering_market = Some(decision.triggering_market);
        self.orders[index].decision_price = Some(decision.decision_price);
        self.orders[index].last_decision = Some(decision);
        self.decisions.push(decision);
        self.schedule_fill(index, decision)?;
        Ok(Some(decision))
    }

    fn schedule_fill(
        &mut self,
        index: usize,
        decision: FillDecision<'static>,
    ) -> Result<(), Error> {
        if decision.order_id != self.orders[index].order.order_id {
            return Err(Error::DecisionLedger);
        }
        if decision.quantity > self.orders[index].remaining_quantity() {
            return Err(Error::DecisionLedger);
        }
        self.orders[index].filled_quantity =
            decimal::add(self.orders[index].filled_quantity, decision.quantity)
                .map_err(decimal_error)?;
        self.orders[index].filled_notional = decimal::add(
            self.orders[index].filled_notional,
            decimal::mul(decision.quantity, decision.price).map_err(decimal_error)?,
        )
        .map_err(decimal_error)?;
        self.orders[index].status = if self.orders[index].remaining_quantity() == 0 {
            OrderStatus::Filled
        } else {
            OrderStatus::PartiallyFilled
        };
        self.orders[index].response_pending = true;
        let status = self.orders[index].status;
        let execution_type = if status == OrderStatus::Filled {
            ExecutionType::Fill
        } else {
            ExecutionType::PartialFill
        };
        self.schedule_report(
            index,
            execution_type,
            status,
            decision.quantity,
            Some(decision.price),
        )?;

        let timestamp = self.response_timestamp()?;
        let fill_id = self.next_id("fill");
        let sequence = self.next_sequence();
        let order_id = self.orders[index].order.order_id.to_owned();
        let symbol = self.orders[index].order.symbol.to_owned();
        let side = self.orders[index].order.side;
        self.fill_records.push(FillRecord {
            event_id: fill_id.clone(),
            fill_id: fill_id.clone(),
            order_id: order_id.clone(),
            symbol,
            side,
            quantity: decision.quantity,
            price: decision.price,
            liquidity: decision.liquidity,
            timestamp_ns: timestamp,
            sequence,
        });
        self.enqueue(timestamp, sequence, 5, fill_id, &order_id, None, true);
        if status == OrderStatus::PartiallyFilled
            && self.orders[index].order.time_in_force == TimeInForce::Ioc
        {
            self.schedule_expiry(index, true)?;
        }
        Ok(())
    }

    fn schedule_expiry(&mut self, index: usize, allow_pending: bool) -> Result<(), Error> {
        if !self.orders[index].is_active()
            || (self.orders[index].response_pending && !allow_pending)
        {
            return Ok(());
        }
        self.orders[index].status = OrderStatus::Expired;
        self.orders[index].response_pending = true;
        self.schedule_report(index, ExecutionType::Expired, OrderStatus::Expired, 0, None)
    }

    fn schedule_report(
        &mut self,
        index: usize,
        execution_type: ExecutionType,
        status: OrderStatus,
        last_fill_quantity: i128,
        last_fill_price: Option<i128>,
    ) -> Result<(), Error> {
        let timestamp = self.response_timestamp()?;
        let report_id = self.next_id("report");
        let average = if self.orders[index].filled_quantity == 0 {
            None
        } else {
            Some(
                decimal::div(
                    self.orders[index].filled_notional,
                    self.orders[index].filled_quantity,
                )
                .map_err(decimal_error)?,
            )
        };
        let sequence = self.next_sequence();
        let order_id = self.orders[index].order.order_id.to_owned();
        let order_quantity = self.orders[index].order.quantity;
        self.report_records.push(ReportRecord {
            event_id: report_id.clone(),
            report_id: report_id.clone(),
            order_id: order_id.clone(),
            timestamp_ns: timestamp,
            sequence,
            status,
            execution_type,
            order_quantity,
            filled_quantity: self.orders[index].filled_quantity,
            remaining_quantity: self.orders[index].remaining_quantity(),
            average_fill_price: average,
            last_fill_quantity,
            last_fill_price,
        });
        self.enqueue(
            timestamp,
            sequence,
            4,
            report_id,
            &order_id,
            Some(execution_type),
            false,
        );
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn enqueue(
        &mut self,
        timestamp_ns: i128,
        sequence: u64,
        class_priority: u8,
        event_id: String,
        order_id: &str,
        execution_type: Option<ExecutionType>,
        is_fill: bool,
    ) {
        self.response_queue.push(ScheduledEnvelope {
            timestamp_ns,
            sequence,
            class_priority,
            feed_ordinal: self.feed_ordinal,
            event_id,
            order_id: order_id.to_owned(),
            execution_type,
            is_fill,
        });
        self.feed_ordinal += 1;
    }

    fn is_matching_opportunity(
        &self,
        index: usize,
        snap: MarketEvent<'static>,
    ) -> Result<bool, Error> {
        if matches!(
            self.orders[index].order.order_type,
            OrderType::Stop | OrderType::StopLimit
        ) {
            return Ok(self.orders[index].stop_triggered
                || self.is_triggered(&self.orders[index].order, snap)?);
        }
        Ok(true)
    }

    fn is_triggered(
        &self,
        order: &MarketOrder<'static>,
        snap: MarketEvent<'static>,
    ) -> Result<bool, Error> {
        if self.fill_model.is_simple_touch() {
            return crate::simple_touch::is_triggered(order, &snap).map_err(market_error);
        }
        let Some(stop_price) = order.stop_price else {
            return Ok(false);
        };
        let price: i128 = match snap {
            MarketEvent::Trade(trade) => trade.price,
            MarketEvent::Quote(quote) => {
                if order.side == Side::Buy {
                    quote.ask_price
                } else {
                    quote.bid_price
                }
            }
        };
        Ok(if order.side == Side::Buy {
            price >= stop_price
        } else {
            price <= stop_price
        })
    }

    fn snapshot(&self, index: usize) -> SimOrderState {
        let working = &self.orders[index];
        SimOrderState {
            order: working.order,
            status: working.status,
            filled_quantity: working.filled_quantity,
            remaining_quantity: working.remaining_quantity(),
            armed: working.armed,
            triggering_market: working.triggering_market,
            decision_price: working.decision_price,
            last_decision: working.last_decision,
        }
    }

    fn upsert_latest_market(&mut self, event: MarketEvent<'static>) {
        for (symbol, existing) in &mut self.latest_markets {
            if symbol == event.symbol() {
                *existing = event;
                return;
            }
        }
        self.latest_markets.push((event.symbol().to_owned(), event));
    }

    fn latest_market_for(&self, symbol: &str) -> Option<MarketEvent<'static>> {
        self.latest_markets
            .iter()
            .find(|(candidate, _)| candidate == symbol)
            .map(|(_, event)| *event)
    }

    fn find_used_order_id(&self, order_id: &str) -> Option<usize> {
        self.used_order_ids.iter().position(|used| used == order_id)
    }

    fn working_index(&self, order_id: &str) -> Result<usize, Error> {
        self.orders
            .iter()
            .position(|entry| entry.order.order_id == order_id)
            .ok_or(Error::UnknownOrder)
    }

    fn observe(&mut self, timestamp_ns: i128, sequence: u64) {
        if self.head_ns.is_none_or(|head| timestamp_ns > head) {
            self.head_ns = Some(timestamp_ns);
        }
        self.sequence_head = self.sequence_head.max(sequence as i128);
    }

    fn response_timestamp(&self) -> Result<i128, Error> {
        let mut head = self.head_ns;
        if let Some(clock) = &self.sim_clock {
            let clock_head = clock.borrow().now();
            head = Some(match head {
                Some(head) => head.max(clock_head),
                None => clock_head,
            });
        }
        let base = head.ok_or(Error::NoTimestamp)?;
        Ok(base + self.latency_ns)
    }

    fn next_sequence(&mut self) -> u64 {
        self.sequence_head += 1;
        self.sequence_head as u64
    }

    fn next_id(&mut self, kind: &str) -> String {
        self.ordinal += 1;
        format!("{}-{}-{}", self.venue_name, kind, self.ordinal)
    }
}

fn same_market(a: MarketEvent<'_>, b: MarketEvent<'_>) -> bool {
    a.timestamp_ns() == b.timestamp_ns()
        && a.sequence() == b.sequence()
        && a.event_id() == b.event_id()
}
