//! Deterministic order-management service layer.
//!
//! Ports `harness.oms`: single-leg execution-report state machines, the
//! replacement/session registry, and atomic multi-leg order groups.

pub mod groups;
pub mod manager;
pub mod state;

pub use groups::{
    GroupLegSnapshot, GroupOrderType, GroupSnapshot, GroupStatus, MultiLegOms,
    MultiLegOrderGroup,
};
pub use manager::{ReplaceRecord, ReplaceStatus, SessionAction, SessionDirective, SingleLegOms};
pub use state::{OrderSnapshot, OrderStateMachine};
