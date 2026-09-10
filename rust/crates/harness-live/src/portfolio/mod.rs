//! Virtual-book and shared-account portfolio accounting.
//!
//! Ports `harness.portfolio`: per-strategy virtual books, the shared-account
//! hub, and deterministic option exercise/assignment/expiry settlement.

pub mod book;
pub mod hub;
pub mod options_lifecycle;

pub use book::{BookSnapshot, PositionMarker, PositionSnapshot, VirtualBook};
pub use hub::{AggregatePosition, PortfolioHub, PortfolioSnapshot};
pub use options_lifecycle::{
    intrinsic_value, AssignmentPolicy, ContractSettlement, DaysToExpiry, OptionLifecycle,
    PessimisticAssignment, SettlementKind,
};
