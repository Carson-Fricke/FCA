//! Provider-free storage-cap estimates and runtime pause guardrails.
//!
//! Ports `lake.storage.capacity`: the policy, the observable status, the
//! alert-shaped JSON, and the guard that evaluates current plus staged bytes.
//! Neither path creates a provider or Azure client.

use std::sync::{Arc, Mutex};

use lake_contracts::DataError;
use serde_json::{json, Value};

/// Operations that have independent configured pause switches.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageOperation {
    /// Sending a new backfill message.
    Enqueue,
    /// Fetching and committing staged partition bytes.
    Ingest,
}

impl StorageOperation {
    /// The stable persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Enqueue => "enqueue",
            Self::Ingest => "ingest",
        }
    }
}

/// Stable states suitable for metrics, logs, and alert rules.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageCapState {
    /// Free space remains after the projected write.
    Available,
    /// The projected write reaches the configured capacity exactly.
    AtCapacity,
    /// The projected write is beyond the configured capacity.
    OverCapacity,
    /// The operation is paused by the guard before it starts.
    Paused,
}

impl StorageCapState {
    /// The stable persisted string value.
    pub const fn value(self) -> &'static str {
        match self {
            Self::Available => "available",
            Self::AtCapacity => "at_capacity",
            Self::OverCapacity => "over_capacity",
            Self::Paused => "paused",
        }
    }
}

/// Explicit capacity and operation-specific pause decisions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageCapPolicy {
    /// The configured storage capacity in bytes.
    pub capacity_bytes: u64,
    /// Whether enqueue stops once the cap is reached.
    pub pause_enqueue_at_capacity: bool,
    /// Whether ingest stops once the cap is reached.
    pub pause_ingest_at_capacity: bool,
}

impl StorageCapPolicy {
    /// Return whether the supplied operation is stopped at the cap.
    pub fn pauses(&self, operation: StorageOperation) -> bool {
        match operation {
            StorageOperation::Enqueue => self.pause_enqueue_at_capacity,
            StorageOperation::Ingest => self.pause_ingest_at_capacity,
        }
    }
}

/// Observable result of one capacity evaluation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageCapStatus {
    /// The evaluated operation.
    pub operation: StorageOperation,
    /// The resulting capacity state.
    pub state: StorageCapState,
    /// Bytes currently in storage.
    pub current_bytes: u64,
    /// Current bytes plus the staged bytes under evaluation.
    pub projected_bytes: u64,
    /// The configured capacity in bytes.
    pub capacity_bytes: u64,
    /// Whether the evaluated operation pauses at the cap.
    pub pause_enabled: bool,
    /// The stable reason for a non-available state.
    pub reason: Option<String>,
}

impl StorageCapStatus {
    /// Whether this operation is currently prevented from proceeding.
    pub fn paused(&self) -> bool {
        self.state == StorageCapState::Paused
    }

    /// Whether the estimate has reached or would cross the configured cap.
    pub fn alertable(&self) -> bool {
        self.projected_bytes >= self.capacity_bytes
    }

    /// Return non-negative capacity remaining before the projected write.
    pub fn remaining_bytes(&self) -> u64 {
        self.capacity_bytes.saturating_sub(self.projected_bytes)
    }

    /// Return stable, non-secret fields for an alert or structured log.
    pub fn as_alert(&self) -> Value {
        json!({
            "alertable": self.alertable(),
            "capacity_bytes": self.capacity_bytes,
            "current_bytes": self.current_bytes,
            "operation": self.operation.value(),
            "pause_enabled": self.pause_enabled,
            "projected_bytes": self.projected_bytes,
            "reason": self.reason,
            "state": self.state.value(),
        })
    }
}

/// Boundary for reading current storage usage without owning the reader.
pub trait StorageUsageProvider: Send + Sync {
    /// Return the current storage estimate in bytes.
    fn current_storage(&self) -> Result<u64, DataError>;
}

/// Estimate current storage by summing catalog byte sizes.
pub fn sum_byte_sizes(sizes: impl Iterator<Item = u64>) -> u64 {
    sizes.fold(0u64, u64::saturating_add)
}

/// Evaluate and enforce capacity using injected usage and typed policy.
pub struct StorageCapGuard {
    policy: StorageCapPolicy,
    usage: Arc<dyn StorageUsageProvider>,
    last_status: Mutex<Option<StorageCapStatus>>,
}

impl StorageCapGuard {
    /// Build a guard over an immutable policy and an injected usage reader.
    pub fn new(policy: StorageCapPolicy, usage: Arc<dyn StorageUsageProvider>) -> Self {
        Self {
            policy,
            usage,
            last_status: Mutex::new(None),
        }
    }

    /// Evaluate current plus staged bytes without changing storage.
    pub fn evaluate(
        &self,
        operation: StorageOperation,
        additional_bytes: u64,
    ) -> Result<StorageCapStatus, DataError> {
        let current_bytes = self.usage.current_storage()?;
        let projected_bytes = current_bytes
            .checked_add(additional_bytes)
            .ok_or_else(|| DataError::value("storage projection overflowed"))?;
        let reaches_capacity = projected_bytes >= self.policy.capacity_bytes;
        let would_exceed = projected_bytes > self.policy.capacity_bytes;
        let pause_enabled = self.policy.pauses(operation);
        let state = if pause_enabled && reaches_capacity {
            StorageCapState::Paused
        } else if would_exceed {
            StorageCapState::OverCapacity
        } else if reaches_capacity {
            StorageCapState::AtCapacity
        } else {
            StorageCapState::Available
        };
        let reason = if would_exceed {
            Some("capacity_would_be_exceeded".to_string())
        } else if reaches_capacity {
            Some("capacity_reached".to_string())
        } else {
            None
        };
        let status = StorageCapStatus {
            operation,
            state,
            current_bytes,
            projected_bytes,
            capacity_bytes: self.policy.capacity_bytes,
            pause_enabled,
            reason,
        };
        *self.lock_status() = Some(status.clone());
        Ok(status)
    }

    /// Allow enqueue or return an explicit, alertable conflict.
    pub fn enforce_enqueue(&self) -> Result<StorageCapStatus, DataError> {
        let status = self.evaluate(StorageOperation::Enqueue, 0)?;
        self.reject_paused(status)
    }

    /// Allow ingest or return an explicit, alertable conflict before staged
    /// bytes become visible.
    pub fn enforce_ingest(&self, additional_bytes: u64) -> Result<StorageCapStatus, DataError> {
        let status = self.evaluate(StorageOperation::Ingest, additional_bytes)?;
        self.reject_paused(status)
    }

    /// Return the most recent status, if an operation has been evaluated.
    pub fn last_status(&self) -> Option<StorageCapStatus> {
        self.lock_status().clone()
    }

    fn reject_paused(&self, status: StorageCapStatus) -> Result<StorageCapStatus, DataError> {
        if status.paused() {
            return Err(DataError::Conflict(paused_message(&status)));
        }
        Ok(status)
    }

    fn lock_status(&self) -> std::sync::MutexGuard<'_, Option<StorageCapStatus>> {
        self.last_status
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn paused_message(status: &StorageCapStatus) -> String {
    format!(
        "storage capacity guard paused {}: {} of {} bytes ({})",
        status.operation.value(),
        status.projected_bytes,
        status.capacity_bytes,
        status.reason.as_deref().unwrap_or("capacity limit")
    )
}
