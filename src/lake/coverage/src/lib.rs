//! Coverage classification, gap planning, and the ensure application.
//!
//! This crate is the Rust port of `lake/coverage`: the calendar-aware
//! coverage calculations of `service.py` and the provider-independent ensure
//! use cases of `application.py`.
//!
//! Two semantics are load-bearing and reproduced exactly:
//!
//! 1. **Coverage classification** — `complete | empty | partial | error |
//!    delisted | missing`, with `is_partial` derived from the session clock.
//! 2. **Ensure terminality** — an open session is satisfiable while it trades;
//!    a closed session left `partial` or `error` is not satisfied, but it is a
//!    repairable gap: it fails an ensure only once the ensure job has settled.

mod application;
mod error;
mod selection;
mod service;
pub use selection::{
    classify_option, contracts_or_default, option_coverage, option_ensure_job_id,
    resolve_option_selection, OptionRequestLike, OptionSelection,
};

pub use application::{
    ensure_coverage, ensure_job_id, requested_coverage, EnsureAdmission, EnsureJob, EnsureOutcome,
    EnsureSpec, EnsureSubmission,
};
pub use error::CoverageRequestError;
pub use service::{
    calculate_coverage, sessions_for, CoverageReport, CoverageSession, CoverageState, GapPlan,
    SessionCoverage,
};
