//! Execution-report-driven state and exact ledgers for one order.

use std::collections::HashSet;

use num::Decimal18;

use crate::accounting::ExecutionReport;
use crate::error::LiveError;
use crate::error::LiveResult;
use crate::orders::ExecutionType;
use crate::orders::OrderStatus;
use crate::orders::SingleLegOrder;
use crate::orders::TimeInForce;

const ZERO: Decimal18 = Decimal18::ZERO;

/// Whether a status is reachable from an execution type.
fn execution_allows(execution_type: ExecutionType, status: OrderStatus) -> bool {
    match execution_type {
        ExecutionType::Acknowledged => status == OrderStatus::Accepted,
        ExecutionType::PartialFill => status == OrderStatus::PartiallyFilled,
        ExecutionType::Fill => status == OrderStatus::Filled,
        ExecutionType::Canceled => status == OrderStatus::Canceled,
        ExecutionType::Rejected => status == OrderStatus::Rejected,
        ExecutionType::Expired => status == OrderStatus::Expired,
        ExecutionType::Replaced => status == OrderStatus::Replaced,
        ExecutionType::Status => matches!(
            status,
            OrderStatus::New
                | OrderStatus::Accepted
                | OrderStatus::PartiallyFilled
                | OrderStatus::CancelPending
        ),
    }
}

/// The exact transition key: source status, execution type, target status.
fn transition_allowed(from: OrderStatus, execution_type: ExecutionType, to: OrderStatus) -> bool {
    use ExecutionType as E;
    use OrderStatus as S;
    match (from, execution_type) {
        (S::New, E::Status) => to == S::New,
        (S::New, E::Acknowledged) => to == S::Accepted,
        (S::New, E::Rejected) => to == S::Rejected,
        (S::Accepted, E::Status) => matches!(to, S::Accepted | S::CancelPending),
        (S::Accepted, E::PartialFill) => to == S::PartiallyFilled,
        (S::Accepted, E::Fill) => to == S::Filled,
        (S::Accepted, E::Canceled) => to == S::Canceled,
        (S::Accepted, E::Expired) => to == S::Expired,
        (S::Accepted, E::Replaced) => to == S::Replaced,
        (S::PartiallyFilled, E::Status) => matches!(to, S::PartiallyFilled | S::CancelPending),
        (S::PartiallyFilled, E::PartialFill) => to == S::PartiallyFilled,
        (S::PartiallyFilled, E::Fill) => to == S::Filled,
        (S::PartiallyFilled, E::Canceled) => to == S::Canceled,
        (S::PartiallyFilled, E::Expired) => to == S::Expired,
        (S::PartiallyFilled, E::Replaced) => to == S::Replaced,
        (S::CancelPending, E::Status) => {
            matches!(to, S::CancelPending | S::Accepted | S::PartiallyFilled)
        }
        (S::CancelPending, E::PartialFill) => to == S::PartiallyFilled,
        (S::CancelPending, E::Fill) => to == S::Filled,
        (S::CancelPending, E::Canceled) => to == S::Canceled,
        (S::CancelPending, E::Expired) => to == S::Expired,
        _ => false,
    }
}

fn is_terminal(status: OrderStatus) -> bool {
    matches!(
        status,
        OrderStatus::Filled
            | OrderStatus::Canceled
            | OrderStatus::Rejected
            | OrderStatus::Expired
            | OrderStatus::Replaced
    )
}

/// Immutable lifecycle and exact execution ledger for one order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderSnapshot {
    /// The immutable order request.
    pub order: SingleLegOrder,
    /// Current lifecycle status.
    pub status: OrderStatus,
    /// Cumulative filled quantity.
    pub filled_quantity: Decimal18,
    /// Cumulative remaining quantity.
    pub remaining_quantity: Decimal18,
    /// Cumulative signed notional of fills.
    pub filled_notional: Decimal18,
    /// Cumulative average fill price.
    pub average_fill_price: Option<Decimal18>,
    /// Number of accepted reports.
    pub report_count: usize,
    /// Most recently accepted report.
    pub last_report: Option<ExecutionReport>,
}

impl OrderSnapshot {
    /// The tracked order identifier.
    pub fn order_id(&self) -> &str {
        &self.order.order_id
    }

    /// Whether further lifecycle reports may be accepted.
    pub fn is_active(&self) -> bool {
        !is_terminal(self.status)
    }

    /// Whether the order has reached an irreversible state.
    pub fn is_terminal(&self) -> bool {
        is_terminal(self.status)
    }

    /// The order's time-in-force policy.
    pub fn time_in_force(&self) -> TimeInForce {
        self.order.time_in_force
    }
}

/// Apply validated execution reports to one single-leg order.
#[derive(Clone, Debug)]
pub struct OrderStateMachine {
    snapshot: OrderSnapshot,
    reports: Vec<ExecutionReport>,
    report_ids: HashSet<String>,
}

impl OrderStateMachine {
    /// Establish the local `NEW` state for `order`.
    pub fn new(order: SingleLegOrder) -> Self {
        let remaining_quantity = order.quantity;
        Self {
            snapshot: OrderSnapshot {
                order,
                status: OrderStatus::New,
                filled_quantity: ZERO,
                remaining_quantity,
                filled_notional: ZERO,
                average_fill_price: None,
                report_count: 0,
                last_report: None,
            },
            reports: Vec::new(),
            report_ids: HashSet::new(),
        }
    }

    /// The current immutable snapshot.
    pub fn snapshot(&self) -> &OrderSnapshot {
        &self.snapshot
    }

    /// The immutable order request.
    pub fn order(&self) -> &SingleLegOrder {
        &self.snapshot.order
    }

    /// Accepted reports in application order.
    pub fn reports(&self) -> &[ExecutionReport] {
        &self.reports
    }

    /// Validate and atomically apply one execution report.
    pub fn apply(&mut self, report: ExecutionReport) -> LiveResult<OrderSnapshot> {
        if self.report_ids.contains(&report.report_id) {
            return Err(LiveError::DuplicateExecutionReport(format!(
                "execution report {:?} was already applied",
                report.report_id
            )));
        }
        let next = self.next_snapshot(&report)?;
        self.report_ids.insert(report.report_id.clone());
        self.reports.push(report);
        self.snapshot = next.clone();
        Ok(next)
    }

    fn next_snapshot(&self, report: &ExecutionReport) -> LiveResult<OrderSnapshot> {
        let current = &self.snapshot;
        if report.order_id != current.order.order_id {
            return Err(LiveError::ExecutionLedger(format!(
                "report order_id {:?} does not match {:?}",
                report.order_id, current.order.order_id
            )));
        }
        if report.order_quantity != current.order.quantity {
            return Err(LiveError::ExecutionLedger(
                "report order_quantity does not match the submitted order".into(),
            ));
        }
        if !execution_allows(report.execution_type, report.status) {
            return Err(LiveError::IllegalTransition(format!(
                "{} report cannot declare {}",
                report.execution_type.as_str(),
                report.status.as_str()
            )));
        }
        if !transition_allowed(current.status, report.execution_type, report.status) {
            return Err(LiveError::IllegalTransition(format!(
                "illegal order transition {} -> {} via {}",
                current.status.as_str(),
                report.status.as_str(),
                report.execution_type.as_str()
            )));
        }
        let filled_notional = self.validate_ledger(report)?;
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

    fn validate_ledger(&self, report: &ExecutionReport) -> LiveResult<Decimal18> {
        let current = &self.snapshot;
        let is_fill = matches!(
            report.execution_type,
            ExecutionType::PartialFill | ExecutionType::Fill
        );

        let filled_notional = if is_fill {
            if !report.last_fill_quantity.is_positive() {
                return Err(LiveError::ExecutionLedger(
                    "fill report requires last_fill_quantity".into(),
                ));
            }
            let Some(last_price) = report.last_fill_price else {
                return Err(LiveError::ExecutionLedger(
                    "fill report requires last_fill_price".into(),
                ));
            };
            let expected_filled = current.filled_quantity + report.last_fill_quantity;
            if report.filled_quantity != expected_filled {
                return Err(LiveError::ExecutionLedger(
                    "filled_quantity must increase by last_fill_quantity".into(),
                ));
            }
            current.filled_notional + report.last_fill_quantity * last_price
        } else {
            if report.last_fill_quantity != ZERO {
                return Err(LiveError::ExecutionLedger(
                    "non-fill report cannot contain last_fill_quantity".into(),
                ));
            }
            if report.last_fill_price.is_some() {
                return Err(LiveError::ExecutionLedger(
                    "non-fill report cannot contain last_fill_price".into(),
                ));
            }
            if report.filled_quantity != current.filled_quantity {
                return Err(LiveError::ExecutionLedger(
                    "non-fill report cannot change filled_quantity".into(),
                ));
            }
            current.filled_notional
        };

        let expected_remaining = current.order.quantity - report.filled_quantity;
        if report.remaining_quantity != expected_remaining {
            return Err(LiveError::ExecutionLedger(
                "remaining_quantity does not match the exact order ledger".into(),
            ));
        }

        if report.filled_quantity == ZERO {
            if report.average_fill_price.is_some() {
                return Err(LiveError::ExecutionLedger(
                    "unfilled order cannot have average_fill_price".into(),
                ));
            }
        } else {
            let expected_average = filled_notional / report.filled_quantity;
            if report.average_fill_price != Some(expected_average) {
                return Err(LiveError::ExecutionLedger(
                    "average_fill_price does not match the exact fill ledger".into(),
                ));
            }
        }

        if report.status == OrderStatus::PartiallyFilled
            && !(ZERO < report.filled_quantity && report.filled_quantity < current.order.quantity)
        {
            return Err(LiveError::ExecutionLedger(
                "partially_filled status requires an incomplete positive fill".into(),
            ));
        }
        if report.status == OrderStatus::Filled && report.filled_quantity != current.order.quantity
        {
            return Err(LiveError::ExecutionLedger(
                "filled status requires the complete order quantity".into(),
            ));
        }
        if matches!(
            report.status,
            OrderStatus::New | OrderStatus::Accepted | OrderStatus::Rejected
        ) && report.filled_quantity != ZERO
        {
            return Err(LiveError::ExecutionLedger(format!(
                "{} status cannot have filled quantity",
                report.status.as_str()
            )));
        }
        Ok(filled_notional)
    }
}
