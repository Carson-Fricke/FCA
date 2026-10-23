//! Owns trader supervision journal; lifecycle coordination remains in the parent.
use std::rc::Rc;

use serde_json::json;
use serde_json::Value;

use super::clock::*;
use super::models::*;
use super::snapshots::*;
/// Maximum restart-ledger history entries retained.
const LEDGER_HISTORY_LIMIT: usize = 64;

/// Durable supervision phase, heartbeat, and restart-ledger records.
pub struct SupervisionJournal {
    store: SharedSnapshotStore,
    job_id: String,
    clock: Rc<dyn SupervisionClock>,
}

impl SupervisionJournal {
    /// Bind a journal to one execution job.
    #[must_use]
    pub fn new(
        store: SharedSnapshotStore,
        job_id: impl Into<String>,
        clock: Rc<dyn SupervisionClock>,
    ) -> Self {
        Self {
            store,
            job_id: job_id.into(),
            clock,
        }
    }

    /// The execution job identifier this journal records.
    #[must_use]
    pub fn job_id(&self) -> &str {
        &self.job_id
    }

    pub(super) fn status_key(&self) -> String {
        format!("runs/{}/supervision.json", self.job_id)
    }

    pub(super) fn ledger_key(&self) -> String {
        format!("runs/{}/restarts.json", self.job_id)
    }

    /// The consecutive-fault count on the restart ledger.
    #[must_use]
    pub fn consecutive_faults(&self) -> u32 {
        self.read_ledger()
            .get("consecutive_faults")
            .and_then(Value::as_u64)
            .unwrap_or(0) as u32
    }

    /// The full restart ledger document.
    #[must_use]
    pub fn read_ledger(&self) -> Value {
        self.store
            .read_text(&self.ledger_key())
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| {
                json!({
                    "format_version": 1,
                    "job_id": self.job_id,
                    "consecutive_faults": 0,
                    "history": [],
                })
            })
    }

    /// Replace the supervision status document.
    pub fn record_status(
        &self,
        phase: SupervisionPhase,
        detail: &str,
        market_event_count: u64,
        snapshot_id: Option<&str>,
        heartbeat: bool,
    ) {
        let now = self.clock.now_ns();
        let document = json!({
            "format_version": 1,
            "job_id": self.job_id,
            "phase": phase.value(),
            "detail": detail,
            "updated_at_ns": now,
            "heartbeat": heartbeat,
            "market_event_count": market_event_count,
            "snapshot_id": snapshot_id,
            "consecutive_faults": self.consecutive_faults(),
        });
        if let Ok(text) = serde_json::to_string(&document) {
            self.store.write_text(&self.status_key(), &text);
        }
    }

    /// Fold one terminal boot outcome into the restart ledger.
    pub fn record_boot_outcome(&self, kind: OutcomeKind, detail: &str) {
        let mut ledger = self.read_ledger();
        let faults = ledger
            .get("consecutive_faults")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let faults = match kind {
            OutcomeKind::Fault => faults + 1,
            OutcomeKind::Clean | OutcomeKind::KillSwitch => 0,
            OutcomeKind::PolicyHalt => faults,
        };
        let entry = json!({
            "at_ns": self.clock.now_ns(),
            "outcome": kind.value(),
            "detail": detail,
        });
        let mut history = ledger
            .get("history")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        history.push(entry);
        if history.len() > LEDGER_HISTORY_LIMIT {
            let drop = history.len() - LEDGER_HISTORY_LIMIT;
            history.drain(0..drop);
        }
        ledger = json!({
            "format_version": 1,
            "job_id": self.job_id,
            "consecutive_faults": faults,
            "history": history,
        });
        if let Ok(text) = serde_json::to_string(&ledger) {
            self.store.write_text(&self.ledger_key(), &text);
        }
    }

    /// The latest supervision status document, or `None`.
    #[must_use]
    pub fn read_status(&self) -> Option<Value> {
        self.store
            .read_text(&self.status_key())
            .and_then(|text| serde_json::from_str(&text).ok())
    }
}
