//! Owns trader supervision configuration; lifecycle coordination remains in the parent.

use crate::runtime::paper::PaperSessionPolicy;
use num::Decimal18;
use serde_json::Value;

use super::models::*;
use super::process::*;

/// Validated supervision knobs with deployment-safe defaults.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SupervisorSettings {
    /// Boot recovery policy.
    pub boot_policy: BootPolicy,
    /// Exact initial paper cash.
    pub initial_cash: Decimal18,
    /// Heartbeat cadence in nanoseconds.
    pub heartbeat_cadence_ns: i64,
    /// Kill-switch poll cadence in nanoseconds.
    pub kill_switch_poll_ns: i64,
    /// Consecutive poll failures tolerated before a restartable fault.
    pub kill_switch_stale: u32,
    /// Periodic snapshot cadence in nanoseconds.
    pub snapshot_cadence_ns: i64,
    /// Continuous reconciliation cadence in nanoseconds.
    pub reconcile_cadence_ns: i64,
    /// Maximum wait for the first market event, in nanoseconds.
    pub market_wait_ns: i64,
    /// Feed connect attempts per connect.
    pub connect_attempts: u32,
    /// Feed reconnect backoff in nanoseconds.
    pub connect_backoff_ns: i64,
    /// Consecutive faulty boots tolerated before a crash-loop refusal.
    pub restart_budget: u32,
}

impl Default for SupervisorSettings {
    fn default() -> Self {
        Self {
            boot_policy: BootPolicy::ResumeReconcile,
            initial_cash: Decimal18::from_unscaled(100_000_000_000_000_000_000_000)
                .unwrap_or(Decimal18::ZERO),
            heartbeat_cadence_ns: 60_000_000_000,
            kill_switch_poll_ns: 10_000_000_000,
            kill_switch_stale: 3,
            snapshot_cadence_ns: 60_000_000_000,
            reconcile_cadence_ns: 900_000_000_000,
            market_wait_ns: 900_000_000_000,
            connect_attempts: 2,
            connect_backoff_ns: 2_000_000_000,
            restart_budget: 3,
        }
    }
}

impl SupervisorSettings {
    /// Validate the supervision knobs.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] for any non-positive cadence
    /// or integer.
    pub fn validate(&self) -> Result<(), SupervisionError> {
        if !self.initial_cash.is_positive() {
            return Err(SupervisionError::Configuration(
                "initial_cash must be positive".to_string(),
            ));
        }
        for (name, value) in [
            ("heartbeat_cadence_ns", self.heartbeat_cadence_ns),
            ("kill_switch_poll_ns", self.kill_switch_poll_ns),
            ("snapshot_cadence_ns", self.snapshot_cadence_ns),
            ("reconcile_cadence_ns", self.reconcile_cadence_ns),
            ("market_wait_ns", self.market_wait_ns),
            ("connect_backoff_ns", self.connect_backoff_ns),
        ] {
            if value <= 0 {
                return Err(SupervisionError::Configuration(format!(
                    "{name} must be a positive duration"
                )));
            }
        }
        for (name, value) in [
            ("kill_switch_stale", self.kill_switch_stale),
            ("connect_attempts", self.connect_attempts),
        ] {
            if value < 1 {
                return Err(SupervisionError::Configuration(format!(
                    "{name} must be a positive integer"
                )));
            }
        }
        Ok(())
    }
}

/// The resolved trader configuration the supervisor consumes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TraderConfig {
    /// Configured execution job identifier, when pinned.
    pub job_id: Option<String>,
    /// Deployment name metadata; never the execution identity.
    pub deployment_name: String,
    /// `paper` or `shadow` mode.
    pub mode: String,
    /// Run seed.
    pub seed: u64,
    /// Strategy instance identifiers.
    pub strategy_instance_ids: Vec<String>,
    /// Risk rejection policy.
    pub on_reject: String,
    /// Configured risk rule identifiers.
    pub rule_ids: Vec<String>,
}

impl TraderConfig {
    /// Load and validate one trader configuration document.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when the document is not a
    /// valid trader shell.
    pub fn load(path: &std::path::Path) -> Result<Self, SupervisionError> {
        let loaded = config::load_configuration(path).map_err(|error| {
            SupervisionError::Configuration(format!("configuration could not be loaded: {error}"))
        })?;
        let value = match loaded {
            config::HarnessConfig::Trader(value) => value,
            other => {
                return Err(SupervisionError::Configuration(format!(
                    "configuration shell must be 'trader', not '{}'",
                    other.shell()
                )))
            }
        };
        Self::from_value(&value)
    }

    /// Resolve the typed fields from the canonical configuration tree.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when a required field is
    /// missing or misshaped.
    pub fn from_value(value: &Value) -> Result<Self, SupervisionError> {
        let mode = value
            .get("mode")
            .and_then(Value::as_str)
            .ok_or_else(|| SupervisionError::Configuration("mode is required".to_string()))?;
        if mode != "paper" && mode != "shadow" {
            return Err(SupervisionError::Configuration(format!(
                "supervised sessions support paper and shadow modes, not {mode:?}"
            )));
        }
        let seed = value.get("seed").and_then(Value::as_u64).unwrap_or(0);
        let job_id = match value.get("job_id") {
            None | Some(Value::Null) => None,
            Some(Value::String(text)) => Some(job_identifier(text, "job_id")?),
            Some(_) => {
                return Err(SupervisionError::Configuration(
                    "job_id must be a string".to_string(),
                ))
            }
        };
        let name = value
            .get("deployment")
            .and_then(|deployment| deployment.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("trader");
        let deployment_name = job_identifier(name, "deployment.name")?;
        let strategy_instance_ids = value
            .get("strategy_instances")
            .and_then(Value::as_array)
            .map(|instances| {
                instances
                    .iter()
                    .filter_map(|instance| {
                        instance
                            .get("instance_id")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if strategy_instance_ids.is_empty() {
            return Err(SupervisionError::Configuration(
                "strategy_instances must not be empty".to_string(),
            ));
        }
        let on_reject = value
            .get("risk_pipeline")
            .and_then(|pipeline| pipeline.get("on_reject"))
            .and_then(Value::as_str)
            .unwrap_or("reject_order")
            .to_string();
        let rule_ids = value
            .get("risk_pipeline")
            .and_then(|pipeline| pipeline.get("rules"))
            .and_then(Value::as_array)
            .map(|rules| {
                rules
                    .iter()
                    .filter_map(|rule| {
                        rule.get("rule_id")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        Ok(Self {
            job_id,
            deployment_name,
            mode: mode.to_string(),
            seed,
            strategy_instance_ids,
            on_reject,
            rule_ids,
        })
    }

    /// The paper session policy for this mode.
    #[must_use]
    pub fn session_policy(&self) -> PaperSessionPolicy {
        PaperSessionPolicy::Session
    }
}
