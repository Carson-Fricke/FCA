//! Paper and shadow runtime kernels.
//!
//! The full Python `harness.runtime` package composes replay, venues, the risk
//! pipeline, and the portfolio hub. The staged Rust port provides the
//! deterministic state-store snapshot contract, the startup recovery planner,
//! the reconciliation differ, the go-live gate, and the paper/shadow seams so
//! the runtimes plug in once their kernel ports land.

pub mod go_live;
pub mod paper;
pub mod reconciler;
pub mod recovery;
pub mod shadow;
pub mod state_store;

pub use go_live::{
    clean_streak, evaluate, GateCriteria, GateDecision, GateState, PaperSessionEvidence,
};
pub use paper::{
    PaperBook, PaperOptions, PaperRuntimeShell, PaperSessionPolicy, PaperSessionResult, PaperShell,
    PaperShellRestore, VenueWatermarks,
};
pub use reconciler::{
    reconcile_cash, reconcile_positions, ExpectedPosition, PositionDelta, ReconcileDecision,
    ReconcileSchedule, ReconcileTrigger, ReconciliationDiff, ReconciliationTolerances,
};
pub use recovery::{plan_recovery, BootPolicyKind, RecoveryAction, RecoveryFacts, RecoveryPlan};
pub use shadow::{
    build_report, first_would_be_difference, BrokerIntent, IntentComparison, LivePrint,
    ObserveOnlyBrokerSink, ShadowReport, ShadowVerdict, SingleLegOrder,
};
pub use state_store::{
    decimal_tag, decimal_text, parse_json, BookState, FillState, InMemorySnapshotStore,
    RandomState, RiskState, RuntimeSnapshot, RuntimeStateStore, SnapshotObjectStore,
    SnapshotSchedule, SnapshotTrigger, StateStoreError, StoredSnapshot, StrategyState, VenueState,
    FORMAT_VERSION,
};
