//! Typed publication helpers; callers choose data budgets and fidelity.
use crate::building_blocks::trailing_haar;
use crate::MetricPublisher;
use crate::PublishError;
use crate::StudyOutput;
use serde_json::json;
use serde_json::Value;

/// A scalar-cell evidence table, suitable for grids, comparisons and aligned paths.
pub struct EvidenceTable {
    columns: Vec<String>,
    rows: Vec<Vec<Value>>,
}

impl EvidenceTable {
    pub fn new(columns: &[&str]) -> Self {
        Self {
            columns: columns.iter().map(|s| (*s).into()).collect(),
            rows: Vec::new(),
        }
    }
    pub fn push(&mut self, row: Vec<Value>) -> Result<(), String> {
        let output = StudyOutput::Table {
            columns: self.columns.clone(),
            rows: vec![row.clone()],
        };
        output.validate()?;
        self.rows.push(row);
        Ok(())
    }
    pub fn output(&self) -> StudyOutput {
        StudyOutput::Table {
            columns: self.columns.clone(),
            rows: self.rows.clone(),
        }
    }
    pub fn publish(&self, name: &str, publisher: &mut MetricPublisher) -> Result<(), PublishError> {
        publisher.publish_output(name, &self.output())
    }
    /// Publish observations and their scientific meaning together; no chart choices needed.
    pub fn publish_described(
        &self,
        semantics: super::DatasetSemantics,
        publisher: &mut MetricPublisher,
    ) -> Result<(), PublishError> {
        let name = semantics.dataset.output.clone();
        self.publish(&name, publisher)?;
        publisher.publish_output(
            &format!("{name}/semantics"),
            &StudyOutput::DatasetSemantics { semantics },
        )
    }
}

/// Persist actual trailing Haar coefficients and per-scale energy at declared observations.
/// Call at a bounded cadence; this does not infer price histories or session identities.
pub struct WaveletHistory {
    table: EvidenceTable,
    energy: EvidenceTable,
}

impl Default for WaveletHistory {
    fn default() -> Self {
        Self {
            table: EvidenceTable::new(&[
                "observation_id",
                "session_id",
                "symbol",
                "event_time_ns",
                "available_at_ns",
                "support",
                "coefficient_index",
                "coefficient",
                "energy",
            ]),
            energy: EvidenceTable::new(&[
                "observation_id",
                "session_id",
                "symbol",
                "event_time_ns",
                "available_at_ns",
                "support",
                "energy",
            ]),
        }
    }
}

impl WaveletHistory {
    pub fn observe(
        &mut self,
        session_id: &str,
        symbol: &str,
        event_time_ns: i64,
        available_at_ns: i64,
        values: &[f64],
    ) -> Result<(), String> {
        if session_id.is_empty() || symbol.is_empty() || event_time_ns > available_at_ns {
            return Err("wavelet history requires identity and causal timestamps".into());
        }
        for level in trailing_haar(values)? {
            let energy =
                level.details.iter().map(|v| v * v).sum::<f64>() / level.details.len() as f64;
            self.energy.push(vec![
                json!(format!(
                    "{session_id}/{symbol}/{event_time_ns}/{}",
                    level.support
                )),
                json!(session_id),
                json!(symbol),
                json!(event_time_ns.to_string()),
                json!(available_at_ns.to_string()),
                json!(level.support),
                json!(energy),
            ])?;
            for (index, value) in level.details.iter().enumerate() {
                self.table.push(vec![
                    json!(format!(
                        "{session_id}/{symbol}/{event_time_ns}/{}/{index}",
                        level.support
                    )),
                    json!(session_id),
                    json!(symbol),
                    json!(event_time_ns.to_string()),
                    json!(available_at_ns.to_string()),
                    json!(level.support),
                    json!(index),
                    json!(value),
                    json!(energy),
                ])?;
            }
        }
        Ok(())
    }
    pub fn output(&self) -> StudyOutput {
        self.table.output()
    }
    pub fn energy_output(&self) -> StudyOutput {
        self.energy.output()
    }
    pub fn publish(&self, name: &str, publisher: &mut MetricPublisher) -> Result<(), PublishError> {
        self.table.publish(name, publisher)?;
        self.energy.publish(&format!("{name}/energy"), publisher)
    }
}
