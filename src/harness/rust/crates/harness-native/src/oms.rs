//! Execution-report-driven order management, ported from
//! `src/harness/oms/state.py` and `src/harness/oms/manager.py`.
//!
//! The implementation hosts the state machine (`OrderStateMachine`) and the
//! lifecycle registry (`SingleLegOMS`) with the shared fixed-point kernel in
//! [`crate::decimal`]. Every ledger quantity is the pinned `i128` scale-18
//! type. Status transitions happen only by applying validated execution
//! reports; a rejected report leaves the machine unchanged.

use crate::decimal::{self, DecimalError};
use crate::market::{OrderType, Side, TimeInForce};

/// A checked failure from the OMS.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Allocation failure.
    OutOfMemory,
    /// A required argument was invalid.
    InvalidArgument,
    /// Fixed-point overflow or a non-representable value.
    OutOfRange,
    /// Division by zero or otherwise invalid decimal operation.
    InvalidDecimal,
    /// Order identifier is not tracked by the OMS.
    UnknownOrder,
    /// Order identifier already used or reserved.
    DuplicateOrder,
    /// Execution report identifier applied more than once.
    DuplicateReport,
    /// Execution report cannot follow the current state.
    IllegalTransition,
    /// Cumulative or last-fill report values are inconsistent.
    ExecutionLedger,
    /// Replacement request or report is inconsistent.
    ReplaceConflict,
    /// Session/expiry policy refusal.
    ExpiryPolicy,
}

impl core::fmt::Display for Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfMemory => "out of memory",
            Self::InvalidArgument => "invalid argument",
            Self::OutOfRange => "fixed-point result is outside decimal128(38, 18)",
            Self::InvalidDecimal => "invalid decimal operation",
            Self::UnknownOrder => "unknown order",
            Self::DuplicateOrder => "duplicate order",
            Self::DuplicateReport => "duplicate report",
            Self::IllegalTransition => "illegal transition",
            Self::ExecutionLedger => "execution ledger inconsistency",
            Self::ReplaceConflict => "replace conflict",
            Self::ExpiryPolicy => "expiry policy refusal",
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

/// Order lifecycle status.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OrderStatus {
    /// Locally created, not yet acknowledged.
    New,
    /// Acknowledged by the venue.
    Accepted,
    /// Partially filled.
    PartiallyFilled,
    /// Fully filled.
    Filled,
    /// Cancel requested, not yet confirmed.
    CancelPending,
    /// Canceled.
    Canceled,
    /// Rejected.
    Rejected,
    /// Expired.
    Expired,
    /// Replaced.
    Replaced,
}

/// Execution report type.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ExecutionType {
    /// Acknowledgement.
    Acknowledged,
    /// Partial fill.
    PartialFill,
    /// Full fill.
    Fill,
    /// Cancel confirmation.
    Canceled,
    /// Rejection.
    Rejected,
    /// Expiry.
    Expired,
    /// Replacement confirmation.
    Replaced,
    /// Status heartbeat.
    Status,
}

/// A single-leg order; envelope fields ride on the bus event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SingleLegOrder {
    /// Order identifier.
    pub order_id: String,
    /// Canonical symbol.
    pub symbol: String,
    /// Order side.
    pub side: Side,
    /// Unscaled scale-18 quantity (positive).
    pub quantity: i128,
    /// Order type.
    pub order_type: OrderType,
    /// Time in force.
    pub time_in_force: TimeInForce,
}

/// A venue/OMS report; all quantities are unscaled at scale 18.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExecutionReport {
    /// Report identifier.
    pub report_id: String,
    /// Order identifier.
    pub order_id: String,
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

/// Immutable lifecycle and exact execution ledger for one order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderSnapshot {
    /// The order this snapshot describes.
    pub order: SingleLegOrder,
    /// Current status.
    pub status: OrderStatus,
    /// Cumulative filled quantity.
    pub filled_quantity: i128,
    /// Remaining quantity.
    pub remaining_quantity: i128,
    /// Cumulative filled notional.
    pub filled_notional: i128,
    /// Average fill price.
    pub average_fill_price: Option<i128>,
    /// Number of applied reports.
    pub report_count: usize,
    /// Most recent applied report.
    pub last_report: Option<ExecutionReport>,
}

impl OrderSnapshot {
    /// The order identifier.
    pub fn order_id(&self) -> &str {
        &self.order.order_id
    }

    /// True when the order is not terminal.
    pub fn is_active(&self) -> bool {
        !is_terminal_status(self.status)
    }

    /// True when the order is terminal.
    pub fn is_terminal(&self) -> bool {
        is_terminal_status(self.status)
    }

    /// The order's time in force.
    pub fn time_in_force(&self) -> TimeInForce {
        self.order.time_in_force
    }
}

/// True when the status is terminal.
pub fn is_terminal_status(status: OrderStatus) -> bool {
    matches!(
        status,
        OrderStatus::Filled
            | OrderStatus::Canceled
            | OrderStatus::Rejected
            | OrderStatus::Expired
            | OrderStatus::Replaced
    )
}

/// Statuses each execution type may declare.
pub fn execution_statuses(execution_type: ExecutionType) -> &'static [OrderStatus] {
    match execution_type {
        ExecutionType::Acknowledged => &[OrderStatus::Accepted],
        ExecutionType::PartialFill => &[OrderStatus::PartiallyFilled],
        ExecutionType::Fill => &[OrderStatus::Filled],
        ExecutionType::Canceled => &[OrderStatus::Canceled],
        ExecutionType::Rejected => &[OrderStatus::Rejected],
        ExecutionType::Expired => &[OrderStatus::Expired],
        ExecutionType::Replaced => &[OrderStatus::Replaced],
        ExecutionType::Status => &[
            OrderStatus::New,
            OrderStatus::Accepted,
            OrderStatus::PartiallyFilled,
            OrderStatus::CancelPending,
        ],
    }
}

/// Transitions keyed by execution type as well as status.
fn legal_transitions(from: OrderStatus, via: ExecutionType) -> &'static [OrderStatus] {
    match from {
        OrderStatus::New => match via {
            ExecutionType::Status => &[OrderStatus::New],
            ExecutionType::Acknowledged => &[OrderStatus::Accepted],
            ExecutionType::Rejected => &[OrderStatus::Rejected],
            _ => &[],
        },
        OrderStatus::Accepted => match via {
            ExecutionType::Status => &[OrderStatus::Accepted, OrderStatus::CancelPending],
            ExecutionType::PartialFill => &[OrderStatus::PartiallyFilled],
            ExecutionType::Fill => &[OrderStatus::Filled],
            ExecutionType::Canceled => &[OrderStatus::Canceled],
            ExecutionType::Expired => &[OrderStatus::Expired],
            ExecutionType::Replaced => &[OrderStatus::Replaced],
            _ => &[],
        },
        OrderStatus::PartiallyFilled => match via {
            ExecutionType::Status => &[OrderStatus::PartiallyFilled, OrderStatus::CancelPending],
            ExecutionType::PartialFill => &[OrderStatus::PartiallyFilled],
            ExecutionType::Fill => &[OrderStatus::Filled],
            ExecutionType::Canceled => &[OrderStatus::Canceled],
            ExecutionType::Expired => &[OrderStatus::Expired],
            ExecutionType::Replaced => &[OrderStatus::Replaced],
            _ => &[],
        },
        OrderStatus::CancelPending => match via {
            ExecutionType::Status => &[
                OrderStatus::CancelPending,
                OrderStatus::Accepted,
                OrderStatus::PartiallyFilled,
            ],
            ExecutionType::PartialFill => &[OrderStatus::PartiallyFilled],
            ExecutionType::Fill => &[OrderStatus::Filled],
            ExecutionType::Canceled => &[OrderStatus::Canceled],
            ExecutionType::Expired => &[OrderStatus::Expired],
            _ => &[],
        },
        OrderStatus::Filled
        | OrderStatus::Canceled
        | OrderStatus::Rejected
        | OrderStatus::Expired
        | OrderStatus::Replaced => &[],
    }
}

/// True when `haystack` contains `needle`.
pub fn contains_status(haystack: &[OrderStatus], needle: OrderStatus) -> bool {
    haystack.contains(&needle)
}

/// Apply validated execution reports to one single-leg order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderStateMachine {
    /// Current immutable snapshot.
    pub snapshot: OrderSnapshot,
    reports: Vec<ExecutionReport>,
}

impl OrderStateMachine {
    /// Create a machine for a locally new order.
    pub fn new(order: SingleLegOrder) -> Self {
        Self {
            snapshot: OrderSnapshot {
                remaining_quantity: order.quantity,
                order,
                status: OrderStatus::New,
                filled_quantity: 0,
                filled_notional: 0,
                average_fill_price: None,
                report_count: 0,
                last_report: None,
            },
            reports: Vec::new(),
        }
    }

    /// The applied reports in application order.
    pub fn reports(&self) -> &[ExecutionReport] {
        &self.reports
    }

    /// Validate and atomically apply one execution report.
    pub fn apply(&mut self, report: ExecutionReport) -> Result<OrderSnapshot, Error> {
        if report.report_id.is_empty() {
            return Err(Error::InvalidArgument);
        }
        if self
            .reports
            .iter()
            .any(|applied| applied.report_id == report.report_id)
        {
            return Err(Error::DuplicateReport);
        }
        let next = self.next_snapshot(&report)?;
        self.reports.push(report);
        self.snapshot = next.clone();
        Ok(next)
    }

    fn next_snapshot(&self, report: &ExecutionReport) -> Result<OrderSnapshot, Error> {
        let current = &self.snapshot;
        if report.order_id.is_empty() {
            return Err(Error::InvalidArgument);
        }
        if report.order_id != current.order.order_id {
            return Err(Error::ExecutionLedger);
        }
        if report.order_quantity != current.order.quantity {
            return Err(Error::ExecutionLedger);
        }
        if !contains_status(execution_statuses(report.execution_type), report.status) {
            return Err(Error::IllegalTransition);
        }
        if !contains_status(
            legal_transitions(current.status, report.execution_type),
            report.status,
        ) {
            return Err(Error::IllegalTransition);
        }
        let filled_notional = validated_notional(current, report)?;
        Ok(OrderSnapshot {
            order: current.order.clone(),
            status: report.status,
            filled_quantity: report.filled_quantity,
            remaining_quantity: report.remaining_quantity,
            filled_notional,
            average_fill_price: report.average_fill_price,
            report_count: current.report_count + 1,
            last_report: Some(report.clone()),
        })
    }
}

fn validated_notional(current: &OrderSnapshot, report: &ExecutionReport) -> Result<i128, Error> {
    let is_fill = matches!(
        report.execution_type,
        ExecutionType::PartialFill | ExecutionType::Fill
    );
    let filled_notional = if is_fill {
        if report.last_fill_quantity <= 0 {
            return Err(Error::ExecutionLedger);
        }
        let last_price = report.last_fill_price.ok_or(Error::ExecutionLedger)?;
        let expected_filled = decimal::add(current.filled_quantity, report.last_fill_quantity)
            .map_err(decimal_error)?;
        if report.filled_quantity != expected_filled {
            return Err(Error::ExecutionLedger);
        }
        decimal::add(
            current.filled_notional,
            decimal::mul(report.last_fill_quantity, last_price).map_err(decimal_error)?,
        )
        .map_err(decimal_error)?
    } else {
        if report.last_fill_quantity != 0 {
            return Err(Error::ExecutionLedger);
        }
        if report.last_fill_price.is_some() {
            return Err(Error::ExecutionLedger);
        }
        if report.filled_quantity != current.filled_quantity {
            return Err(Error::ExecutionLedger);
        }
        current.filled_notional
    };

    let expected_remaining =
        decimal::sub(current.order.quantity, report.filled_quantity).map_err(decimal_error)?;
    if report.remaining_quantity != expected_remaining {
        return Err(Error::ExecutionLedger);
    }

    if report.filled_quantity == 0 {
        if report.average_fill_price.is_some() {
            return Err(Error::ExecutionLedger);
        }
    } else {
        let expected_average =
            decimal::div(filled_notional, report.filled_quantity).map_err(decimal_error)?;
        if report.average_fill_price != Some(expected_average) {
            return Err(Error::ExecutionLedger);
        }
    }

    if report.status == OrderStatus::PartiallyFilled
        && !(report.filled_quantity > 0 && report.filled_quantity < current.order.quantity)
    {
        return Err(Error::ExecutionLedger);
    }
    if report.status == OrderStatus::Filled && report.filled_quantity != current.order.quantity {
        return Err(Error::ExecutionLedger);
    }
    if matches!(
        report.status,
        OrderStatus::New | OrderStatus::Accepted | OrderStatus::Rejected
    ) && report.filled_quantity != 0
    {
        return Err(Error::ExecutionLedger);
    }
    Ok(filled_notional)
}

/// Whether an average should be computed or supplied explicitly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AverageArg {
    /// Compute the ledger-exact average.
    Computed,
    /// Use an explicit value.
    Explicit(Option<i128>),
}

/// The computed-average sentinel.
pub const fn computed_average() -> AverageArg {
    AverageArg::Computed
}

/// Build a report whose ledger values follow from `snapshot`.
pub fn report_from(
    snapshot: &OrderSnapshot,
    status: OrderStatus,
    execution_type: ExecutionType,
    report_id: &str,
    last_quantity: i128,
    last_price: Option<i128>,
    average: AverageArg,
) -> Result<ExecutionReport, Error> {
    let mut notional = snapshot.filled_notional;
    if let Some(price) = last_price {
        notional = decimal::add(
            notional,
            decimal::mul(last_quantity, price).map_err(decimal_error)?,
        )
        .map_err(decimal_error)?;
    }
    let filled = decimal::add(snapshot.filled_quantity, last_quantity).map_err(decimal_error)?;
    let calculated = if filled == 0 {
        None
    } else {
        Some(decimal::div(notional, filled).map_err(decimal_error)?)
    };
    Ok(ExecutionReport {
        report_id: report_id.to_owned(),
        order_id: snapshot.order.order_id.clone(),
        status,
        execution_type,
        order_quantity: snapshot.order.quantity,
        filled_quantity: filled,
        remaining_quantity: decimal::sub(snapshot.order.quantity, filled).map_err(decimal_error)?,
        average_fill_price: match average {
            AverageArg::Computed => calculated,
            AverageArg::Explicit(value) => value,
        },
        last_fill_quantity: last_quantity,
        last_fill_price: last_price,
    })
}

/// Lifecycle of an atomic replacement request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReplaceStatus {
    /// Reserved, awaiting a `REPLACED` report.
    Pending,
    /// Confirmed by a `REPLACED` report.
    Applied,
    /// Aborted because the original reached a terminal state.
    Aborted,
}

/// Action required for an active order at a session boundary.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionAction {
    /// Emit an expiry report.
    Expire,
    /// Carry the order forward with no transition.
    Carry,
}

/// A reserved replacement and its report-confirmed outcome.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReplaceRecord {
    /// The order being replaced.
    pub original_order_id: String,
    /// The reserved replacement order.
    pub replacement: SingleLegOrder,
    /// Current reservation status.
    pub status: ReplaceStatus,
    /// Report that confirmed or aborted the replacement.
    pub report_id: Option<String>,
}

impl ReplaceRecord {
    /// The replacement order identifier.
    pub fn replacement_order_id(&self) -> &str {
        &self.replacement.order_id
    }
}

/// A deterministic session-boundary policy decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionDirective {
    /// The order the directive applies to.
    pub order_id: String,
    /// The required action.
    pub action: SessionAction,
}

#[derive(Clone, Debug)]
struct LinkEntry {
    from: String,
    to: String,
}

/// Track single-leg orders using execution reports as the mutation path.
#[derive(Clone, Debug, Default)]
pub struct SingleLegOMS {
    machines: Vec<OrderStateMachine>,
    used_order_ids: Vec<String>,
    replacements: Vec<(String, ReplaceRecord)>,
    replaced_by_links: Vec<LinkEntry>,
    replaces_links: Vec<LinkEntry>,
}

impl SingleLegOMS {
    /// Create an empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a validated local order in `NEW` state.
    pub fn submit(&mut self, order: SingleLegOrder) -> Result<OrderSnapshot, Error> {
        if order.order_id.is_empty() || order.symbol.is_empty() {
            return Err(Error::InvalidArgument);
        }
        if order.quantity <= 0 {
            return Err(Error::InvalidArgument);
        }
        if self.find_used_order_id(&order.order_id).is_some() {
            return Err(Error::DuplicateOrder);
        }
        self.used_order_ids.push(order.order_id.clone());
        let machine = OrderStateMachine::new(order);
        let snapshot = machine.snapshot.clone();
        self.machines.push(machine);
        Ok(snapshot)
    }

    /// Route one report and atomically confirm any replacement.
    pub fn apply(&mut self, report: ExecutionReport) -> Result<OrderSnapshot, Error> {
        let machine_index = self.machine_index(&report.order_id)?;

        if report.execution_type == ExecutionType::Replaced {
            let replacement_index = self
                .replacement_index(&report.order_id)
                .ok_or(Error::ReplaceConflict)?;
            if self.replacements[replacement_index].1.status != ReplaceStatus::Pending {
                return Err(Error::ReplaceConflict);
            }
            let replacement_order = self.replacements[replacement_index].1.replacement.clone();
            let replacement_machine = OrderStateMachine::new(replacement_order);
            let snapshot = self.machines[machine_index].apply(report.clone())?;
            let replacement_id = self.replacements[replacement_index]
                .1
                .replacement
                .order_id
                .clone();
            self.replaced_by_links.push(LinkEntry {
                from: report.order_id.clone(),
                to: replacement_id.clone(),
            });
            self.replaces_links.push(LinkEntry {
                from: replacement_id,
                to: report.order_id.clone(),
            });
            self.replacements[replacement_index].1.status = ReplaceStatus::Applied;
            self.replacements[replacement_index].1.report_id = Some(report.report_id.clone());
            self.machines.push(replacement_machine);
            return Ok(snapshot);
        }

        let snapshot = self.machines[machine_index].apply(report.clone())?;
        if let Some(replacement_index) = self.replacement_index(&report.order_id) {
            if self.replacements[replacement_index].1.status == ReplaceStatus::Pending
                && snapshot.is_terminal()
            {
                self.replacements[replacement_index].1.status = ReplaceStatus::Aborted;
                self.replacements[replacement_index].1.report_id = Some(report.report_id);
            }
        }
        Ok(snapshot)
    }

    /// Reserve a replacement without changing either order's lifecycle.
    pub fn request_replace(
        &mut self,
        original_order_id: &str,
        replacement_order: SingleLegOrder,
    ) -> Result<ReplaceRecord, Error> {
        let machine_index = self.machine_index(original_order_id)?;
        let original = &self.machines[machine_index].snapshot;
        if !matches!(
            original.status,
            OrderStatus::Accepted | OrderStatus::PartiallyFilled
        ) {
            return Err(Error::ReplaceConflict);
        }
        if self.replacement_index(original_order_id).is_some() {
            return Err(Error::ReplaceConflict);
        }
        if self
            .find_used_order_id(&replacement_order.order_id)
            .is_some()
        {
            return Err(Error::DuplicateOrder);
        }
        if replacement_order.symbol != original.order.symbol {
            return Err(Error::ReplaceConflict);
        }
        if replacement_order.side != original.order.side {
            return Err(Error::ReplaceConflict);
        }
        let record = ReplaceRecord {
            original_order_id: original_order_id.to_owned(),
            replacement: replacement_order,
            status: ReplaceStatus::Pending,
            report_id: None,
        };
        self.replacements
            .push((original_order_id.to_owned(), record.clone()));
        self.used_order_ids
            .push(record.replacement.order_id.clone());
        Ok(record)
    }

    /// Return an order's current immutable state.
    pub fn state(&self, order_id: &str) -> Result<OrderSnapshot, Error> {
        let index = self.machine_index(order_id)?;
        Ok(self.machines[index].snapshot.clone())
    }

    /// Return all accepted reports for an order in application order.
    pub fn reports(&self, order_id: &str) -> Result<&[ExecutionReport], Error> {
        let index = self.machine_index(order_id)?;
        Ok(self.machines[index].reports())
    }

    /// Return replacement tracking for an original order, if present.
    pub fn replacement(&self, original_order_id: &str) -> Option<ReplaceRecord> {
        let index = self.replacement_index(original_order_id)?;
        Some(self.replacements[index].1.clone())
    }

    /// Return the report-confirmed replacement order identifier.
    pub fn replaced_by(&self, order_id: &str) -> Option<&str> {
        link_target(&self.replaced_by_links, order_id)
    }

    /// Return the order identifier replaced by this order.
    pub fn replaces(&self, order_id: &str) -> Option<&str> {
        link_target(&self.replaces_links, order_id)
    }

    /// Return the confirmed replacement chain containing `order_id`.
    pub fn replacement_chain(&self, order_id: &str) -> Result<Vec<String>, Error> {
        self.machine_index(order_id)?;
        let mut first = order_id;
        while let Some(previous) = link_target(&self.replaces_links, first) {
            first = previous;
        }
        let mut chain = vec![first.to_owned()];
        while let Some(next) = link_target(&self.replaced_by_links, &chain[chain.len() - 1]) {
            chain.push(next.to_owned());
        }
        Ok(chain)
    }

    /// Return active orders in deterministic registration order.
    pub fn active_states(&self) -> Vec<OrderSnapshot> {
        self.machines
            .iter()
            .filter(|machine| machine.snapshot.is_active())
            .map(|machine| machine.snapshot.clone())
            .collect()
    }

    /// Describe session-close handling without mutating order state.
    pub fn session_directives(&self) -> Vec<SessionDirective> {
        self.machines
            .iter()
            .filter(|machine| {
                machine.snapshot.is_active() && machine.snapshot.status != OrderStatus::New
            })
            .map(|machine| SessionDirective {
                order_id: machine.snapshot.order.order_id.clone(),
                action: if machine.snapshot.order.time_in_force == TimeInForce::Gtc {
                    SessionAction::Carry
                } else {
                    SessionAction::Expire
                },
            })
            .collect()
    }

    /// Return IOC orders requiring expiry after a matching opportunity.
    pub fn ioc_expiry_directives(&self) -> Vec<SessionDirective> {
        self.machines
            .iter()
            .filter(|machine| {
                machine.snapshot.order.time_in_force == TimeInForce::Ioc
                    && machine.snapshot.is_active()
                    && machine.snapshot.status != OrderStatus::New
            })
            .map(|machine| SessionDirective {
                order_id: machine.snapshot.order.order_id.clone(),
                action: SessionAction::Expire,
            })
            .collect()
    }

    /// Apply a Day/IOC session expiry report, refusing to expire GTC.
    pub fn apply_session_expiry(
        &mut self,
        report: ExecutionReport,
    ) -> Result<OrderSnapshot, Error> {
        validate_expiry_report(&report)?;
        let snapshot = self.state(&report.order_id)?;
        if snapshot.time_in_force() == TimeInForce::Gtc {
            return Err(Error::ExpiryPolicy);
        }
        self.apply(report)
    }

    /// Apply the expiry report emitted after an IOC matching opportunity.
    pub fn apply_ioc_expiry(&mut self, report: ExecutionReport) -> Result<OrderSnapshot, Error> {
        validate_expiry_report(&report)?;
        let snapshot = self.state(&report.order_id)?;
        if snapshot.time_in_force() != TimeInForce::Ioc {
            return Err(Error::ExpiryPolicy);
        }
        self.apply(report)
    }

    fn machine_index(&self, order_id: &str) -> Result<usize, Error> {
        self.machines
            .iter()
            .position(|machine| machine.snapshot.order.order_id == order_id)
            .ok_or(Error::UnknownOrder)
    }

    fn find_used_order_id(&self, order_id: &str) -> Option<usize> {
        self.used_order_ids.iter().position(|used| used == order_id)
    }

    fn replacement_index(&self, original_order_id: &str) -> Option<usize> {
        self.replacements
            .iter()
            .position(|(original, _)| original == original_order_id)
    }
}

fn link_target<'a>(links: &'a [LinkEntry], from: &str) -> Option<&'a str> {
    links
        .iter()
        .find(|link| link.from == from)
        .map(|link| link.to.as_str())
}

fn validate_expiry_report(report: &ExecutionReport) -> Result<(), Error> {
    if report.report_id.is_empty() || report.order_id.is_empty() {
        return Err(Error::InvalidArgument);
    }
    if report.execution_type != ExecutionType::Expired || report.status != OrderStatus::Expired {
        return Err(Error::ExpiryPolicy);
    }
    Ok(())
}

/// True when two snapshots agree on every externally visible ledger field.
pub fn snapshots_equal(a: &OrderSnapshot, b: &OrderSnapshot) -> bool {
    a.status == b.status
        && a.filled_quantity == b.filled_quantity
        && a.remaining_quantity == b.remaining_quantity
        && a.filled_notional == b.filled_notional
        && a.average_fill_price == b.average_fill_price
        && a.report_count == b.report_count
        && a.last_report.is_none() == b.last_report.is_none()
}

#[cfg(test)]
mod tests {
    use super::{
        computed_average, report_from, snapshots_equal, AverageArg, Error, ExecutionType,
        OrderSnapshot, OrderStateMachine, OrderStatus, ReplaceStatus, SessionAction, SingleLegOMS,
        SingleLegOrder,
    };
    use crate::decimal::from_int;
    use crate::market::{OrderType, Side, TimeInForce};

    fn ten() -> i128 {
        from_int(10)
    }

    fn test_order(order_id: &str) -> SingleLegOrder {
        SingleLegOrder {
            order_id: order_id.to_owned(),
            symbol: "AAPL".to_owned(),
            side: Side::Buy,
            quantity: ten(),
            order_type: OrderType::Limit,
            time_in_force: TimeInForce::Day,
        }
    }

    fn manager_order(
        order_id: &str,
        time_in_force: TimeInForce,
        side: Side,
        symbol: &str,
        quantity: i128,
    ) -> SingleLegOrder {
        SingleLegOrder {
            order_id: order_id.to_owned(),
            symbol: symbol.to_owned(),
            side,
            quantity,
            order_type: OrderType::Limit,
            time_in_force,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn report_for(
        machine: &OrderStateMachine,
        status: OrderStatus,
        execution_type: ExecutionType,
        report_id: &str,
        last_quantity: i128,
        last_price: Option<i128>,
        average: AverageArg,
    ) -> Result<super::ExecutionReport, Error> {
        report_from(
            &machine.snapshot,
            status,
            execution_type,
            report_id,
            last_quantity,
            last_price,
            average,
        )
    }

    fn apply_ack(machine: &mut OrderStateMachine, report_id: &str) -> Result<(), Error> {
        machine.apply(report_for(
            machine,
            OrderStatus::Accepted,
            ExecutionType::Acknowledged,
            report_id,
            0,
            None,
            computed_average(),
        )?)?;
        Ok(())
    }

    fn apply_partial(machine: &mut OrderStateMachine, report_id: &str) -> Result<(), Error> {
        machine.apply(report_for(
            machine,
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            report_id,
            from_int(2),
            Some(from_int(100) + 250_000_000_000_000_000),
            computed_average(),
        )?)?;
        Ok(())
    }

    fn machine_in_status(status: OrderStatus) -> Result<OrderStateMachine, Error> {
        let mut machine = OrderStateMachine::new(test_order("source"));
        match status {
            OrderStatus::New => {}
            OrderStatus::Rejected => {
                machine.apply(report_for(
                    &machine,
                    OrderStatus::Rejected,
                    ExecutionType::Rejected,
                    "setup-rejected",
                    0,
                    None,
                    computed_average(),
                )?)?;
            }
            OrderStatus::Accepted => apply_ack(&mut machine, "setup-ack")?,
            OrderStatus::PartiallyFilled => {
                apply_ack(&mut machine, "setup-ack")?;
                apply_partial(&mut machine, "setup-partial")?;
            }
            OrderStatus::CancelPending => {
                apply_ack(&mut machine, "setup-ack")?;
                machine.apply(report_for(
                    &machine,
                    OrderStatus::CancelPending,
                    ExecutionType::Status,
                    "setup-cancel-pending",
                    0,
                    None,
                    computed_average(),
                )?)?;
            }
            OrderStatus::Filled => {
                apply_ack(&mut machine, "setup-ack")?;
                machine.apply(report_for(
                    &machine,
                    OrderStatus::Filled,
                    ExecutionType::Fill,
                    "setup-filled",
                    ten(),
                    Some(from_int(100)),
                    computed_average(),
                )?)?;
            }
            OrderStatus::Canceled => {
                apply_ack(&mut machine, "setup-ack")?;
                machine.apply(report_for(
                    &machine,
                    OrderStatus::Canceled,
                    ExecutionType::Canceled,
                    "setup-canceled",
                    0,
                    None,
                    computed_average(),
                )?)?;
            }
            OrderStatus::Expired => {
                apply_ack(&mut machine, "setup-ack")?;
                machine.apply(report_for(
                    &machine,
                    OrderStatus::Expired,
                    ExecutionType::Expired,
                    "setup-expired",
                    0,
                    None,
                    computed_average(),
                )?)?;
            }
            OrderStatus::Replaced => {
                apply_ack(&mut machine, "setup-ack")?;
                machine.apply(report_for(
                    &machine,
                    OrderStatus::Replaced,
                    ExecutionType::Replaced,
                    "setup-replaced",
                    0,
                    None,
                    computed_average(),
                )?)?;
            }
        }
        Ok(machine)
    }

    fn candidate_report(
        target: OrderStatus,
        machine: &OrderStateMachine,
    ) -> Result<super::ExecutionReport, Error> {
        let execution_type = match target {
            OrderStatus::New => ExecutionType::Status,
            OrderStatus::Accepted => {
                if machine.snapshot.status == OrderStatus::New {
                    ExecutionType::Acknowledged
                } else {
                    ExecutionType::Status
                }
            }
            OrderStatus::PartiallyFilled => ExecutionType::PartialFill,
            OrderStatus::Filled => ExecutionType::Fill,
            OrderStatus::CancelPending => ExecutionType::Status,
            OrderStatus::Canceled => ExecutionType::Canceled,
            OrderStatus::Rejected => ExecutionType::Rejected,
            OrderStatus::Expired => ExecutionType::Expired,
            OrderStatus::Replaced => ExecutionType::Replaced,
        };
        let mut last_quantity: i128 = 0;
        let mut last_price: Option<i128> = None;
        match target {
            OrderStatus::PartiallyFilled => {
                if machine.snapshot.remaining_quantity > 0 {
                    last_quantity = from_int(1).min(machine.snapshot.remaining_quantity);
                    last_price = Some(from_int(100));
                }
            }
            OrderStatus::Filled => {
                last_quantity = machine.snapshot.remaining_quantity;
                if last_quantity > 0 {
                    last_price = Some(from_int(100));
                }
            }
            _ => {}
        }
        report_for(
            machine,
            target,
            execution_type,
            "candidate",
            last_quantity,
            last_price,
            computed_average(),
        )
    }

    fn legal_targets(source: OrderStatus) -> &'static [OrderStatus] {
        match source {
            OrderStatus::New => &[
                OrderStatus::New,
                OrderStatus::Accepted,
                OrderStatus::Rejected,
            ],
            OrderStatus::Accepted => &[
                OrderStatus::Accepted,
                OrderStatus::PartiallyFilled,
                OrderStatus::Filled,
                OrderStatus::CancelPending,
                OrderStatus::Canceled,
                OrderStatus::Expired,
                OrderStatus::Replaced,
            ],
            OrderStatus::PartiallyFilled => &[
                OrderStatus::PartiallyFilled,
                OrderStatus::Filled,
                OrderStatus::CancelPending,
                OrderStatus::Canceled,
                OrderStatus::Expired,
                OrderStatus::Replaced,
            ],
            OrderStatus::CancelPending => &[
                OrderStatus::Accepted,
                OrderStatus::PartiallyFilled,
                OrderStatus::Filled,
                OrderStatus::CancelPending,
                OrderStatus::Canceled,
                OrderStatus::Expired,
            ],
            OrderStatus::Filled
            | OrderStatus::Canceled
            | OrderStatus::Rejected
            | OrderStatus::Expired
            | OrderStatus::Replaced => &[],
        }
    }

    const ALL_STATUSES: [OrderStatus; 9] = [
        OrderStatus::New,
        OrderStatus::Accepted,
        OrderStatus::PartiallyFilled,
        OrderStatus::Filled,
        OrderStatus::CancelPending,
        OrderStatus::Canceled,
        OrderStatus::Rejected,
        OrderStatus::Expired,
        OrderStatus::Replaced,
    ];

    #[test]
    fn all_order_types_and_time_in_force_values_share_the_lifecycle() {
        let order_types = [
            OrderType::Market,
            OrderType::Limit,
            OrderType::Stop,
            OrderType::StopLimit,
        ];
        let tifs = [TimeInForce::Day, TimeInForce::Gtc, TimeInForce::Ioc];
        for order_type in order_types {
            for time_in_force in tifs {
                let order = SingleLegOrder {
                    order_id: format!("{order_type:?}-{time_in_force:?}"),
                    symbol: "AAPL".to_owned(),
                    side: Side::Buy,
                    quantity: ten(),
                    order_type,
                    time_in_force,
                };
                let mut machine = OrderStateMachine::new(order);
                apply_ack(&mut machine, "ack").unwrap();
                let partial = machine
                    .apply(
                        report_for(
                            &machine,
                            OrderStatus::PartiallyFilled,
                            ExecutionType::PartialFill,
                            "partial",
                            from_int(3),
                            Some(from_int(100)),
                            computed_average(),
                        )
                        .unwrap(),
                    )
                    .unwrap();
                let filled = machine
                    .apply(
                        report_for(
                            &machine,
                            OrderStatus::Filled,
                            ExecutionType::Fill,
                            "fill",
                            from_int(7),
                            Some(from_int(101)),
                            computed_average(),
                        )
                        .unwrap(),
                    )
                    .unwrap();
                assert_eq!(partial.status, OrderStatus::PartiallyFilled);
                assert_eq!(filled.status, OrderStatus::Filled);
                assert_eq!(filled.filled_quantity, ten());
                assert_eq!(filled.average_fill_price, Some(100_700_000_000_000_000_000));
                assert_eq!(machine.snapshot.order.order_type, order_type);
                assert_eq!(machine.snapshot.order.time_in_force, time_in_force);
            }
        }
    }

    #[test]
    fn partial_fills_and_cancel_preserve_exact_decimal_ledgers() {
        let mut machine = OrderStateMachine::new(test_order("order-1"));
        apply_ack(&mut machine, "ack").unwrap();
        machine
            .apply(
                report_for(
                    &machine,
                    OrderStatus::PartiallyFilled,
                    ExecutionType::PartialFill,
                    "partial-1",
                    125_000_000_000_000_000,
                    Some(10_010_000_000_000_000_000),
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();
        let partial = machine
            .apply(
                report_for(
                    &machine,
                    OrderStatus::PartiallyFilled,
                    ExecutionType::PartialFill,
                    "partial-2",
                    375_000_000_000_000_000,
                    Some(10_030_000_000_000_000_000),
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();
        machine
            .apply(
                report_for(
                    &machine,
                    OrderStatus::CancelPending,
                    ExecutionType::Status,
                    "cancel-pending",
                    0,
                    None,
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();
        let canceled = machine
            .apply(
                report_for(
                    &machine,
                    OrderStatus::Canceled,
                    ExecutionType::Canceled,
                    "canceled",
                    0,
                    None,
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();

        assert_eq!(partial.filled_quantity, 500_000_000_000_000_000);
        assert_eq!(partial.remaining_quantity, 9_500_000_000_000_000_000);
        assert_eq!(partial.filled_notional, 5_012_500_000_000_000_000);
        assert_eq!(partial.average_fill_price, Some(10_025_000_000_000_000_000));
        assert_eq!(partial.filled_quantity, canceled.filled_quantity);
        assert_eq!(partial.remaining_quantity, canceled.remaining_quantity);
        assert_eq!(partial.filled_notional, canceled.filled_notional);
        assert_eq!(partial.average_fill_price, canceled.average_fill_price);
    }

    #[test]
    fn invalid_report_is_atomic_and_duplicate_report_id_is_rejected() {
        let mut machine = OrderStateMachine::new(test_order("order-1"));
        apply_ack(&mut machine, "ack").unwrap();
        let before = machine.snapshot.clone();

        let bad_average = report_for(
            &machine,
            OrderStatus::PartiallyFilled,
            ExecutionType::PartialFill,
            "bad-average",
            crate::decimal::ONE,
            Some(from_int(100)),
            AverageArg::Explicit(Some(from_int(99) + 990_000_000_000_000_000)),
        )
        .unwrap();
        assert_eq!(machine.apply(bad_average), Err(Error::ExecutionLedger));
        assert!(snapshots_equal(&before, &machine.snapshot));
        assert_eq!(machine.reports().len(), 1);

        let duplicate = report_for(
            &machine,
            OrderStatus::Accepted,
            ExecutionType::Status,
            "ack",
            0,
            None,
            computed_average(),
        )
        .unwrap();
        assert_eq!(machine.apply(duplicate), Err(Error::DuplicateReport));
        assert!(snapshots_equal(&before, &machine.snapshot));
    }

    #[test]
    fn execution_type_cannot_masquerade_as_another_status() {
        let cases = [
            (ExecutionType::Acknowledged, OrderStatus::Canceled),
            (ExecutionType::PartialFill, OrderStatus::Filled),
            (ExecutionType::Fill, OrderStatus::PartiallyFilled),
            (ExecutionType::Canceled, OrderStatus::Expired),
            (ExecutionType::Rejected, OrderStatus::Accepted),
            (ExecutionType::Expired, OrderStatus::Canceled),
            (ExecutionType::Replaced, OrderStatus::Canceled),
            (ExecutionType::Status, OrderStatus::Filled),
        ];
        for (execution_type, status) in cases {
            let mut machine = OrderStateMachine::new(test_order("order-1"));
            let before = machine.snapshot.clone();
            let mismatch = report_for(
                &machine,
                status,
                execution_type,
                "mismatch",
                0,
                None,
                computed_average(),
            )
            .unwrap();
            assert_eq!(machine.apply(mismatch), Err(Error::IllegalTransition));
            assert!(snapshots_equal(&before, &machine.snapshot));
        }
    }

    #[test]
    fn transition_matrix_is_exhaustive() {
        for source in ALL_STATUSES {
            for target in ALL_STATUSES {
                let mut machine = machine_in_status(source).unwrap();
                let candidate = candidate_report(target, &machine).unwrap();
                if legal_targets(source).contains(&target) {
                    let applied = machine.apply(candidate).unwrap();
                    assert_eq!(applied.status, target);
                } else {
                    let before = machine.snapshot.clone();
                    assert_eq!(machine.apply(candidate), Err(Error::IllegalTransition));
                    assert!(snapshots_equal(&before, &machine.snapshot));
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn oms_report(
        oms: &SingleLegOMS,
        order_id: &str,
        status: OrderStatus,
        execution_type: ExecutionType,
        report_id: &str,
        last_quantity: i128,
        last_price: Option<i128>,
        average: AverageArg,
    ) -> Result<super::ExecutionReport, Error> {
        report_from(
            &oms.state(order_id)?,
            status,
            execution_type,
            report_id,
            last_quantity,
            last_price,
            average,
        )
    }

    fn acknowledge(oms: &mut SingleLegOMS, order_id: &str, report_id: &str) -> Result<(), Error> {
        let report = oms_report(
            oms,
            order_id,
            OrderStatus::Accepted,
            ExecutionType::Acknowledged,
            report_id,
            0,
            None,
            computed_average(),
        )?;
        oms.apply(report)?;
        Ok(())
    }

    fn expire_report(
        oms: &SingleLegOMS,
        order_id: &str,
        report_id: &str,
    ) -> Result<super::ExecutionReport, Error> {
        oms_report(
            oms,
            order_id,
            OrderStatus::Expired,
            ExecutionType::Expired,
            report_id,
            0,
            None,
            computed_average(),
        )
    }

    #[test]
    fn replace_is_reserved_then_confirmed_atomically_by_report() {
        let mut oms = SingleLegOMS::new();
        oms.submit(test_order("original")).unwrap();
        acknowledge(&mut oms, "original", "ack-original").unwrap();
        oms.apply(
            oms_report(
                &oms,
                "original",
                OrderStatus::PartiallyFilled,
                ExecutionType::PartialFill,
                "partial-original",
                1_250_000_000_000_000_000,
                Some(from_int(100) + 100_000_000_000_000_000),
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
        let replacement = manager_order(
            "replacement",
            TimeInForce::Day,
            Side::Buy,
            "AAPL",
            from_int(6),
        );

        let pending = oms.request_replace("original", replacement).unwrap();
        assert_eq!(pending.status, ReplaceStatus::Pending);
        assert_eq!(
            oms.state("original").unwrap().status,
            OrderStatus::PartiallyFilled
        );
        assert!(oms.replaced_by("original").is_none());
        assert_eq!(oms.state("replacement"), Err(Error::UnknownOrder));

        let replaced = oms
            .apply(
                oms_report(
                    &oms,
                    "original",
                    OrderStatus::Replaced,
                    ExecutionType::Replaced,
                    "replaced-original",
                    0,
                    None,
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();

        assert_eq!(replaced.status, OrderStatus::Replaced);
        assert_eq!(replaced.filled_quantity, 1_250_000_000_000_000_000);
        assert_eq!(
            replaced.average_fill_price,
            Some(100_100_000_000_000_000_000)
        );
        assert_eq!(oms.state("replacement").unwrap().status, OrderStatus::New);
        assert_eq!(oms.replaced_by("original"), Some("replacement"));
        assert_eq!(oms.replaces("replacement"), Some("original"));

        let chain = oms.replacement_chain("replacement").unwrap();
        assert_eq!(chain.len(), 2);
        assert_eq!(chain[0], "original");
        assert_eq!(chain[1], "replacement");

        let applied = oms.replacement("original").unwrap();
        assert_eq!(applied.status, ReplaceStatus::Applied);
        assert_eq!(applied.report_id.as_deref(), Some("replaced-original"));

        let active = oms.active_states();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].order_id(), "replacement");
    }

    #[test]
    fn failed_replace_report_changes_nothing_and_keeps_reservation() {
        let mut oms = SingleLegOMS::new();
        oms.submit(test_order("original")).unwrap();
        acknowledge(&mut oms, "original", "ack-original").unwrap();
        oms.apply(
            oms_report(
                &oms,
                "original",
                OrderStatus::PartiallyFilled,
                ExecutionType::PartialFill,
                "partial",
                crate::decimal::ONE,
                Some(from_int(100)),
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
        oms.request_replace("original", test_order("replacement"))
            .unwrap();
        let before = oms.state("original").unwrap();
        let invalid = oms_report(
            &oms,
            "original",
            OrderStatus::Replaced,
            ExecutionType::Replaced,
            "bad-replaced",
            0,
            None,
            AverageArg::Explicit(Some(from_int(99))),
        )
        .unwrap();

        assert_eq!(oms.apply(invalid), Err(Error::ExecutionLedger));
        assert!(snapshots_equal(&before, &oms.state("original").unwrap()));
        let pending = oms.replacement("original").unwrap();
        assert_eq!(pending.status, ReplaceStatus::Pending);
        assert_eq!(oms.state("replacement"), Err(Error::UnknownOrder));
    }

    #[test]
    fn replace_requires_accepted_compatible_order_and_pending_request() {
        let mut oms = SingleLegOMS::new();
        oms.submit(test_order("original")).unwrap();

        assert_eq!(
            oms.request_replace("original", test_order("too-early")),
            Err(Error::ReplaceConflict)
        );

        acknowledge(&mut oms, "original", "ack-original").unwrap();
        assert_eq!(
            oms.request_replace(
                "original",
                manager_order("wrong-symbol", TimeInForce::Day, Side::Buy, "MSFT", ten())
            ),
            Err(Error::ReplaceConflict)
        );
        assert_eq!(
            oms.request_replace(
                "original",
                manager_order("wrong-side", TimeInForce::Day, Side::Sell, "AAPL", ten())
            ),
            Err(Error::ReplaceConflict)
        );

        let without_pending = oms_report(
            &oms,
            "original",
            OrderStatus::Replaced,
            ExecutionType::Replaced,
            "without-pending",
            0,
            None,
            computed_average(),
        )
        .unwrap();
        assert_eq!(oms.apply(without_pending), Err(Error::ReplaceConflict));
        assert_eq!(oms.state("original").unwrap().status, OrderStatus::Accepted);
    }

    #[test]
    fn terminal_report_aborts_pending_replace_and_preserves_cancel_ledger() {
        let mut oms = SingleLegOMS::new();
        oms.submit(test_order("original")).unwrap();
        acknowledge(&mut oms, "original", "ack-original").unwrap();
        oms.request_replace("original", test_order("reserved"))
            .unwrap();

        oms.apply(
            oms_report(
                &oms,
                "original",
                OrderStatus::CancelPending,
                ExecutionType::Status,
                "cancel-pending",
                0,
                None,
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
        let canceled = oms
            .apply(
                oms_report(
                    &oms,
                    "original",
                    OrderStatus::Canceled,
                    ExecutionType::Canceled,
                    "canceled",
                    0,
                    None,
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();

        assert_eq!(canceled.filled_quantity, 0);
        assert_eq!(canceled.remaining_quantity, ten());
        let replacement = oms.replacement("original").unwrap();
        assert_eq!(replacement.status, ReplaceStatus::Aborted);
        assert_eq!(replacement.report_id.as_deref(), Some("canceled"));
        assert_eq!(oms.state("reserved"), Err(Error::UnknownOrder));
    }

    #[test]
    fn ioc_expiry_and_gtc_session_carry_over() {
        let mut oms = SingleLegOMS::new();
        oms.submit(manager_order(
            "local-new",
            TimeInForce::Gtc,
            Side::Buy,
            "AAPL",
            ten(),
        ))
        .unwrap();
        oms.submit(manager_order(
            "day",
            TimeInForce::Day,
            Side::Buy,
            "AAPL",
            ten(),
        ))
        .unwrap();
        oms.submit(manager_order(
            "gtc",
            TimeInForce::Gtc,
            Side::Buy,
            "AAPL",
            ten(),
        ))
        .unwrap();
        oms.submit(manager_order(
            "ioc",
            TimeInForce::Ioc,
            Side::Buy,
            "AAPL",
            ten(),
        ))
        .unwrap();
        acknowledge(&mut oms, "day", "ack-day").unwrap();
        acknowledge(&mut oms, "gtc", "ack-gtc").unwrap();
        acknowledge(&mut oms, "ioc", "ack-ioc").unwrap();

        let directives = oms.session_directives();
        assert_eq!(directives.len(), 3);
        assert_eq!(directives[0].order_id, "day");
        assert_eq!(directives[0].action, SessionAction::Expire);
        assert_eq!(directives[1].order_id, "gtc");
        assert_eq!(directives[1].action, SessionAction::Carry);
        assert_eq!(directives[2].order_id, "ioc");
        assert_eq!(directives[2].action, SessionAction::Expire);

        let ioc_directives = oms.ioc_expiry_directives();
        assert_eq!(ioc_directives.len(), 1);
        assert_eq!(ioc_directives[0].order_id, "ioc");
        assert_eq!(ioc_directives[0].action, SessionAction::Expire);

        oms.apply(
            oms_report(
                &oms,
                "ioc",
                OrderStatus::PartiallyFilled,
                ExecutionType::PartialFill,
                "ioc-partial",
                2_500_000_000_000_000_000,
                Some(from_int(100) + 50_000_000_000_000_000),
                computed_average(),
            )
            .unwrap(),
        )
        .unwrap();
        let ioc_expired = oms
            .apply_ioc_expiry(expire_report(&oms, "ioc", "expired-ioc").unwrap())
            .unwrap();
        let day_expired = oms
            .apply_session_expiry(expire_report(&oms, "day", "expired-day").unwrap())
            .unwrap();

        assert_eq!(ioc_expired.status, OrderStatus::Expired);
        assert_eq!(ioc_expired.filled_quantity, 2_500_000_000_000_000_000);
        assert_eq!(ioc_expired.remaining_quantity, 7_500_000_000_000_000_000);
        assert_eq!(
            ioc_expired.average_fill_price,
            Some(100_050_000_000_000_000_000)
        );
        assert_eq!(day_expired.status, OrderStatus::Expired);
        assert_eq!(oms.state("gtc").unwrap().status, OrderStatus::Accepted);
        assert_eq!(oms.state("local-new").unwrap().status, OrderStatus::New);

        let later = oms.session_directives();
        assert_eq!(later.len(), 1);
        assert_eq!(later[0].order_id, "gtc");
        assert_eq!(later[0].action, SessionAction::Carry);

        let filled = oms
            .apply(
                oms_report(
                    &oms,
                    "gtc",
                    OrderStatus::Filled,
                    ExecutionType::Fill,
                    "gtc-next-session-fill",
                    ten(),
                    Some(from_int(102) + 150_000_000_000_000_000),
                    computed_average(),
                )
                .unwrap(),
            )
            .unwrap();
        assert_eq!(filled.status, OrderStatus::Filled);
        assert_eq!(filled.average_fill_price, Some(102_150_000_000_000_000_000));
    }

    #[test]
    fn expiry_policy_refusals() {
        let mut oms = SingleLegOMS::new();
        oms.submit(manager_order(
            "gtc",
            TimeInForce::Gtc,
            Side::Buy,
            "AAPL",
            ten(),
        ))
        .unwrap();
        oms.submit(manager_order(
            "day",
            TimeInForce::Day,
            Side::Buy,
            "AAPL",
            ten(),
        ))
        .unwrap();
        acknowledge(&mut oms, "gtc", "ack-gtc").unwrap();
        acknowledge(&mut oms, "day", "ack-day").unwrap();

        assert_eq!(
            oms.apply_session_expiry(expire_report(&oms, "gtc", "expired-gtc").unwrap()),
            Err(Error::ExpiryPolicy)
        );
        assert_eq!(
            oms.apply_ioc_expiry(expire_report(&oms, "day", "expired-day").unwrap()),
            Err(Error::ExpiryPolicy)
        );
        let not_expiry = oms_report(
            &oms,
            "day",
            OrderStatus::Canceled,
            ExecutionType::Canceled,
            "not-expiry",
            0,
            None,
            computed_average(),
        )
        .unwrap();
        assert_eq!(oms.apply_ioc_expiry(not_expiry), Err(Error::ExpiryPolicy));

        assert_eq!(oms.state("gtc").unwrap().status, OrderStatus::Accepted);
        assert_eq!(oms.state("day").unwrap().status, OrderStatus::Accepted);
    }

    #[test]
    fn snapshots_compare_on_visible_fields() {
        let a = OrderSnapshot {
            order: test_order("a"),
            status: OrderStatus::New,
            filled_quantity: 0,
            remaining_quantity: ten(),
            filled_notional: 0,
            average_fill_price: None,
            report_count: 0,
            last_report: None,
        };
        let mut b = a.clone();
        b.order.order_id = "b".to_owned();
        assert!(snapshots_equal(&a, &b));
    }
}
