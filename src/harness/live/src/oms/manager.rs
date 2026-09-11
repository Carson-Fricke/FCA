//! Single-leg order registry, replacement tracking, and expiry policy.

use std::collections::{HashMap, HashSet};

use crate::accounting::ExecutionReport;
use crate::error::{LiveError, LiveResult};
use crate::oms::state::{OrderSnapshot, OrderStateMachine};
use crate::orders::{ExecutionType, OrderStatus, SingleLegOrder, TimeInForce};

/// Lifecycle of an atomic replacement request.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ReplaceStatus {
    /// Reserved, awaiting a venue report.
    Pending,
    /// Confirmed by a `REPLACED` report.
    Applied,
    /// The original order terminated before replacement.
    Aborted,
}

/// Action required for an active order at a session boundary.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SessionAction {
    /// Emit an expiry report.
    Expire,
    /// Carry the order across the boundary.
    Carry,
}

/// A reserved replacement and its report-confirmed outcome.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReplaceRecord {
    /// The original order identifier.
    pub original_order_id: String,
    /// The reserved replacement order.
    pub replacement: SingleLegOrder,
    /// Reservation lifecycle.
    pub status: ReplaceStatus,
    /// The confirming report identifier.
    pub report_id: Option<String>,
}

impl ReplaceRecord {
    /// The reserved replacement identifier.
    pub fn replacement_order_id(&self) -> &str {
        &self.replacement.order_id
    }
}

/// A deterministic session-boundary policy decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionDirective {
    /// The order the directive applies to.
    pub order_id: String,
    /// Expire or carry.
    pub action: SessionAction,
}

/// Track single-leg orders using execution reports as the mutation path.
#[derive(Clone, Debug, Default)]
pub struct SingleLegOms {
    machines: HashMap<String, OrderStateMachine>,
    order: Vec<String>,
    used_order_ids: HashSet<String>,
    replacements: HashMap<String, ReplaceRecord>,
    replaced_by: HashMap<String, String>,
    replaces: HashMap<String, String>,
}

impl SingleLegOms {
    /// Construct an empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a validated local order in `NEW` state.
    pub fn submit(&mut self, order: SingleLegOrder) -> LiveResult<OrderSnapshot> {
        if self.used_order_ids.contains(&order.order_id) {
            return Err(LiveError::DuplicateOrder(format!(
                "order_id {:?} was already used",
                order.order_id
            )));
        }
        let order_id = order.order_id.clone();
        let machine = OrderStateMachine::new(order);
        let snapshot = machine.snapshot().clone();
        self.machines.insert(order_id.clone(), machine);
        self.used_order_ids.insert(order_id.clone());
        self.order.push(order_id);
        Ok(snapshot)
    }

    /// Route one report and atomically confirm any replacement.
    pub fn apply(&mut self, report: ExecutionReport) -> LiveResult<OrderSnapshot> {
        let order_id = report.order_id.clone();
        let report_id = report.report_id.clone();

        if report.execution_type == ExecutionType::Replaced {
            let Some(record) = self.replacements.get(&order_id).cloned() else {
                return Err(LiveError::Replace(format!(
                    "order {order_id:?} has no pending replacement"
                )));
            };
            if record.status != ReplaceStatus::Pending {
                return Err(LiveError::Replace(format!(
                    "order {order_id:?} has no pending replacement"
                )));
            }
            let replacement_machine = OrderStateMachine::new(record.replacement.clone());
            let snapshot = self.machine_mut(&order_id)?.apply(report)?;
            let replacement_id = record.replacement.order_id.clone();
            self.machines
                .insert(replacement_id.clone(), replacement_machine);
            self.replaced_by
                .insert(order_id.clone(), replacement_id.clone());
            self.replaces
                .insert(replacement_id.clone(), order_id.clone());
            self.order.push(replacement_id);
            if let Some(entry) = self.replacements.get_mut(&order_id) {
                entry.status = ReplaceStatus::Applied;
                entry.report_id = Some(report_id);
            }
            return Ok(snapshot);
        }

        let snapshot = self.machine_mut(&order_id)?.apply(report)?;
        if let Some(record) = self.replacements.get_mut(&order_id) {
            if record.status == ReplaceStatus::Pending && snapshot.is_terminal() {
                record.status = ReplaceStatus::Aborted;
                record.report_id = Some(report_id);
            }
        }
        Ok(snapshot)
    }

    /// Reserve a replacement without changing either order's lifecycle.
    pub fn request_replace(
        &mut self,
        original_order_id: &str,
        replacement: SingleLegOrder,
    ) -> LiveResult<ReplaceRecord> {
        let original = self.machine(original_order_id)?.snapshot().clone();
        if !matches!(
            original.status,
            OrderStatus::Accepted | OrderStatus::PartiallyFilled
        ) {
            return Err(LiveError::Replace(format!(
                "cannot replace order in {} state",
                original.status.as_str()
            )));
        }
        if self.replacements.contains_key(original_order_id) {
            return Err(LiveError::Replace(format!(
                "order {original_order_id:?} was already replaced"
            )));
        }
        if self.used_order_ids.contains(&replacement.order_id) {
            return Err(LiveError::DuplicateOrder(format!(
                "order_id {:?} was already used",
                replacement.order_id
            )));
        }
        if replacement.symbol != original.order.symbol {
            return Err(LiveError::Replace(
                "replacement cannot change symbol".into(),
            ));
        }
        if replacement.side != original.order.side {
            return Err(LiveError::Replace("replacement cannot change side".into()));
        }
        let record = ReplaceRecord {
            original_order_id: original_order_id.to_string(),
            replacement,
            status: ReplaceStatus::Pending,
            report_id: None,
        };
        self.replacements
            .insert(original_order_id.to_string(), record.clone());
        self.used_order_ids
            .insert(record.replacement.order_id.clone());
        Ok(record)
    }

    /// Return an order's current immutable state.
    pub fn state(&self, order_id: &str) -> LiveResult<OrderSnapshot> {
        Ok(self.machine(order_id)?.snapshot().clone())
    }

    /// Return all accepted reports for an order.
    pub fn reports(&self, order_id: &str) -> LiveResult<Vec<ExecutionReport>> {
        Ok(self.machine(order_id)?.reports().to_vec())
    }

    /// Return replacement tracking for an original order, if present.
    pub fn replacement(&self, original_order_id: &str) -> Option<&ReplaceRecord> {
        self.replacements.get(original_order_id)
    }

    /// Return the report-confirmed replacement identifier.
    pub fn replaced_by(&self, order_id: &str) -> Option<&str> {
        self.replaced_by.get(order_id).map(String::as_str)
    }

    /// Return the identifier replaced by this order.
    pub fn replaces(&self, order_id: &str) -> Option<&str> {
        self.replaces.get(order_id).map(String::as_str)
    }

    /// Return the confirmed replacement chain containing `order_id`.
    pub fn replacement_chain(&self, order_id: &str) -> LiveResult<Vec<String>> {
        self.machine(order_id)?;
        let mut first = order_id.to_string();
        while let Some(previous) = self.replaces.get(&first) {
            first = previous.clone();
        }
        let mut chain = vec![first.clone()];
        while let Some(next) = self.replaced_by.get(chain.last().unwrap()) {
            chain.push(next.clone());
        }
        Ok(chain)
    }

    /// Return active orders in deterministic registration order.
    pub fn active_states(&self) -> Vec<OrderSnapshot> {
        self.order
            .iter()
            .filter_map(|id| self.machines.get(id))
            .filter(|machine| machine.snapshot().is_active())
            .map(|machine| machine.snapshot().clone())
            .collect()
    }

    /// Describe session-close handling without mutating order state.
    pub fn session_directives(&self) -> Vec<SessionDirective> {
        self.active_states()
            .into_iter()
            .filter(|snapshot| snapshot.status != OrderStatus::New)
            .map(|snapshot| SessionDirective {
                action: if snapshot.time_in_force() == TimeInForce::Gtc {
                    SessionAction::Carry
                } else {
                    SessionAction::Expire
                },
                order_id: snapshot.order_id().to_string(),
            })
            .collect()
    }

    /// Return IOC orders requiring expiry after a matching opportunity.
    pub fn ioc_expiry_directives(&self) -> Vec<SessionDirective> {
        self.active_states()
            .into_iter()
            .filter(|snapshot| {
                snapshot.time_in_force() == TimeInForce::Ioc && snapshot.status != OrderStatus::New
            })
            .map(|snapshot| SessionDirective {
                order_id: snapshot.order_id().to_string(),
                action: SessionAction::Expire,
            })
            .collect()
    }

    /// Apply a Day/IOC session expiry report, refusing to expire GTC.
    pub fn apply_session_expiry(&mut self, report: ExecutionReport) -> LiveResult<OrderSnapshot> {
        Self::validate_expiry_report(&report)?;
        let snapshot = self.state(&report.order_id)?;
        if snapshot.time_in_force() == TimeInForce::Gtc {
            return Err(LiveError::Validation(
                "GTC order carries across session boundaries".into(),
            ));
        }
        self.apply(report)
    }

    /// Apply the expiry report emitted after an IOC matching opportunity.
    pub fn apply_ioc_expiry(&mut self, report: ExecutionReport) -> LiveResult<OrderSnapshot> {
        Self::validate_expiry_report(&report)?;
        let snapshot = self.state(&report.order_id)?;
        if snapshot.time_in_force() != TimeInForce::Ioc {
            return Err(LiveError::Validation(
                "IOC expiry requires an IOC order".into(),
            ));
        }
        self.apply(report)
    }

    fn machine(&self, order_id: &str) -> LiveResult<&OrderStateMachine> {
        self.machines
            .get(order_id)
            .ok_or_else(|| LiveError::UnknownOrder(order_id.to_string()))
    }

    fn machine_mut(&mut self, order_id: &str) -> LiveResult<&mut OrderStateMachine> {
        self.machines
            .get_mut(order_id)
            .ok_or_else(|| LiveError::UnknownOrder(order_id.to_string()))
    }

    fn validate_expiry_report(report: &ExecutionReport) -> LiveResult<()> {
        if report.execution_type != ExecutionType::Expired || report.status != OrderStatus::Expired
        {
            return Err(LiveError::Validation(
                "expiry policy requires an expired execution report".into(),
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "../../tests/unit/oms_manager.rs"]
mod tests;
