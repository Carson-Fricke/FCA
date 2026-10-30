//! Wire contracts for replay-sealed features and host-resolved forward returns.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// An authored request contains values, never a decision timestamp or row ID.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureRequest {
    pub dataset: String,
    pub symbol: String,
    pub features: BTreeMap<String, f64>,
    /// Exact elapsed horizon on the capture frame's availability clock.
    pub horizon_ns: i64,
}

/// Host-only publication. Nanosecond timestamps and IDs are decimal strings.
/// Features are sealed during replay; outcome columns are attached by the host.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CausalObservations {
    pub horizon_ns: String,
    pub feature_columns: Vec<String>,
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Value>>,
}

/// The fixed host-owned columns preceding feature columns.
pub const OBSERVATION_COLUMNS: [&str; 10] = [
    "observation_id",
    "symbol",
    "interval",
    "decision_at_ns",
    "outcome_due_at_ns",
    "outcome_available_at_ns",
    "reference_close",
    "outcome_close",
    "outcome_return_bps",
    "outcome_status",
];

/// Names cannot escape the reserved host namespace or collide with metadata.
pub fn valid_capture_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-' | b'.'))
}

impl CausalObservations {
    /// Validate the current publication contract, including timing and outcomes.
    pub fn validate(&self) -> Result<(), String> {
        let horizon: i64 = self
            .horizon_ns
            .parse()
            .map_err(|_| "invalid causal horizon")?;
        let expected: Vec<String> = OBSERVATION_COLUMNS
            .into_iter()
            .map(str::to_string)
            .chain(self.feature_columns.iter().cloned())
            .collect();
        if horizon <= 0
            || self.feature_columns.is_empty()
            || self
                .feature_columns
                .windows(2)
                .any(|pair| pair[0] >= pair[1])
            || self.feature_columns.iter().any(|name| {
                !name
                    .strip_prefix("feature/")
                    .is_some_and(valid_capture_name)
            })
            || self.columns != expected
        {
            return Err("invalid causal observation schema".into());
        }
        let mut ids = std::collections::BTreeSet::new();
        for row in &self.rows {
            if row.len() != expected.len() {
                return Err("invalid causal observation row width".into());
            }
            let time = |index: usize| -> Result<i64, String> {
                row[index]
                    .as_str()
                    .and_then(|v| v.parse().ok())
                    .ok_or_else(|| "causal timestamps must be decimal strings".into())
            };
            let decision = time(3)?;
            let due = time(4)?;
            let available = if row[5].is_null() {
                None
            } else {
                Some(time(5)?)
            };
            if !row[0]
                .as_str()
                .is_some_and(|id| !id.is_empty() && ids.insert(id))
                || [1, 2, 6]
                    .into_iter()
                    .any(|i| !row[i].as_str().is_some_and(|s| !s.is_empty()))
                || decision.checked_add(horizon) != Some(due)
                || available.is_some_and(|value| value < due)
                || row[10..]
                    .iter()
                    .any(|v| !v.as_f64().is_some_and(f64::is_finite))
            {
                return Err("invalid causal observation identity, features or timing".into());
            }
            let valid = match row[9].as_str() {
                Some("observed") => {
                    available == Some(due)
                        && row[7].as_str().is_some_and(|v| !v.is_empty())
                        && row[8].as_f64().is_some_and(f64::is_finite)
                }
                Some("missing") => available.is_some() && row[7].is_null() && row[8].is_null(),
                Some("end_of_run") => available.is_none() && row[7].is_null() && row[8].is_null(),
                _ => false,
            };
            if !valid {
                return Err("invalid causal outcome state".into());
            }
        }
        Ok(())
    }
}
