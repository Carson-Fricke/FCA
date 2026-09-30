//! Deterministic sweep ranking and parameter sensitivity statistics.
//!
//! Port of `harness.sweeps.sensitivity`. Both statistics consume cataloged run
//! rows joined against a sweep's [`SweepPoint`](super::spec::SweepPoint) values,
//! use exact fixed-point arithmetic, and order every output with deterministic
//! tie-breaks, so identical inputs always produce identical reports.

use std::collections::BTreeMap;

use num::Decimal18;

use crate::canonical_json::JsonValue;

/// A sweep statistics failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SensitivityError {
    /// The metric name was blank.
    BlankMetric,
    /// No cataloged run carried the metric.
    NoRuns(String),
}

impl std::fmt::Display for SensitivityError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BlankMetric => formatter.write_str("metric must be a non-empty string"),
            Self::NoRuns(metric) => {
                write!(formatter, "no cataloged runs carry the metric {metric:?}")
            }
        }
    }
}

impl std::error::Error for SensitivityError {}

/// One cataloged run row joined against the sweep.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunRecord {
    /// Run identifier.
    pub run_id: String,
    /// Optional configuration digest.
    pub configuration_digest: Option<String>,
    /// String-exact headline metrics.
    pub metrics: BTreeMap<String, String>,
}

fn metric_value(record: &RunRecord, metric: &str) -> Option<Decimal18> {
    record
        .metrics
        .get(metric)
        .and_then(|raw| raw.parse::<Decimal18>().ok())
}

/// One ranked catalog row for the requested headline metric.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RankedRun {
    /// One-based ordinal rank.
    pub rank: usize,
    /// Run identifier.
    pub run_id: String,
    /// Metric value.
    pub value: Decimal18,
    /// Optional configuration digest.
    pub configuration_digest: Option<String>,
}

/// Rank catalog rows by one metric with deterministic tie-breaks.
///
/// # Errors
///
/// Returns [`SensitivityError::BlankMetric`] when `metric` is blank.
pub fn rank_runs(
    records: &[RunRecord],
    metric: &str,
    descending: bool,
) -> Result<Vec<RankedRun>, SensitivityError> {
    if metric.trim().is_empty() {
        return Err(SensitivityError::BlankMetric);
    }
    let mut scored: Vec<(&RunRecord, Decimal18)> = records
        .iter()
        .filter_map(|record| metric_value(record, metric).map(|value| (record, value)))
        .collect();
    scored.sort_by(|left, right| {
        let ordering = if descending {
            right.1.cmp(&left.1)
        } else {
            left.1.cmp(&right.1)
        };
        ordering.then_with(|| left.0.run_id.cmp(&right.0.run_id))
    });
    Ok(scored
        .into_iter()
        .enumerate()
        .map(|(index, (record, value))| RankedRun {
            rank: index + 1,
            run_id: record.run_id.clone(),
            value,
            configuration_digest: record.configuration_digest.clone(),
        })
        .collect())
}

/// Per-level mean of the metric over the runs sharing that level.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParameterLevelMean {
    /// Canonical level text.
    pub value: String,
    /// Arithmetic mean of the metric.
    pub mean: Decimal18,
    /// Number of runs at this level.
    pub count: usize,
}

/// One-way main-effect summary for one swept parameter.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParameterSensitivity {
    /// Parameter name.
    pub name: String,
    /// Level means, sorted by level text.
    pub levels: Vec<ParameterLevelMean>,
    /// Grand mean across the usable runs.
    pub grand_mean: Decimal18,
    /// Effect range `max(mean) - min(mean)`.
    pub effect_range: Decimal18,
    /// Best level, ties broken by level text ascending.
    pub best_value: String,
}

/// Complete sensitivity summary of one sweep for one metric.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepSensitivity {
    /// Metric name.
    pub metric: String,
    /// Number of runs used.
    pub runs_used: usize,
    /// Grand mean across the usable runs.
    pub grand_mean: Decimal18,
    /// Per-parameter summaries in sorted name order.
    pub parameters: Vec<ParameterSensitivity>,
}

/// Return the canonical level label for one parameter value.
#[must_use]
pub fn level_text(value: &JsonValue) -> String {
    value.to_canonical_string_utf8()
}

fn mean(values: &[Decimal18]) -> Decimal18 {
    let mut total = Decimal18::ZERO;
    for value in values {
        total = total.checked_add(*value).unwrap_or(total);
    }
    let count = Decimal18::from_i64(values.len() as i64);
    total.checked_div(count).unwrap_or(total)
}

/// Summarize per-parameter effects from the sweep's cataloged results.
///
/// # Errors
///
/// Returns [`SensitivityError::BlankMetric`] for a blank metric or
/// [`SensitivityError::NoRuns`] when no usable run carries the metric.
pub fn parameter_sensitivity(
    points: &[super::spec::SweepPoint],
    records: &[RunRecord],
    metric: &str,
) -> Result<SweepSensitivity, SensitivityError> {
    if metric.trim().is_empty() {
        return Err(SensitivityError::BlankMetric);
    }
    let by_run: BTreeMap<&str, &super::spec::SweepPoint> = points
        .iter()
        .map(|point| (point.run_id.as_str(), point))
        .collect();
    let mut usable: Vec<(&super::spec::SweepPoint, Decimal18)> = Vec::new();
    for record in records {
        let Some(point) = by_run.get(record.run_id.as_str()) else {
            continue;
        };
        if let Some(value) = metric_value(record, metric) {
            usable.push((point, value));
        }
    }
    if usable.is_empty() {
        return Err(SensitivityError::NoRuns(metric.to_string()));
    }
    let values: Vec<Decimal18> = usable.iter().map(|(_, value)| *value).collect();
    let grand_mean = mean(&values);
    let mut names: std::collections::BTreeSet<&str> = std::collections::BTreeSet::new();
    for (point, _) in &usable {
        for name in point.parameters.keys() {
            names.insert(name.as_str());
        }
    }
    let parameters = names
        .into_iter()
        .map(|name| summarize_parameter(name, &usable, grand_mean))
        .collect();
    Ok(SweepSensitivity {
        metric: metric.to_string(),
        runs_used: usable.len(),
        grand_mean,
        parameters,
    })
}

fn summarize_parameter(
    name: &str,
    usable: &[(&super::spec::SweepPoint, Decimal18)],
    grand_mean: Decimal18,
) -> ParameterSensitivity {
    let mut grouped: BTreeMap<String, Vec<Decimal18>> = BTreeMap::new();
    for (point, value) in usable {
        if let Some(parameter) = point.parameters.get(name) {
            grouped
                .entry(level_text(parameter))
                .or_default()
                .push(*value);
        }
    }
    let levels: Vec<ParameterLevelMean> = grouped
        .into_iter()
        .map(|(value, values)| ParameterLevelMean {
            value,
            mean: mean(&values),
            count: values.len(),
        })
        .collect();
    let mut best_index = 0usize;
    let mut high = levels[0].mean;
    let mut low = levels[0].mean;
    for (index, level) in levels.iter().enumerate() {
        if level.mean > high {
            high = level.mean;
        }
        if level.mean < low {
            low = level.mean;
        }
        if level.mean > levels[best_index].mean
            || (level.mean == levels[best_index].mean && level.value < levels[best_index].value)
        {
            best_index = index;
        }
    }
    let effect_range = high.checked_sub(low).unwrap_or(Decimal18::ZERO);
    let best_value = levels[best_index].value.clone();
    ParameterSensitivity {
        name: name.to_string(),
        levels,
        grand_mean,
        effect_range,
        best_value,
    }
}
