//! Typed exploratory evidence copied and persisted by the job host.
use std::collections::BTreeMap;
use std::collections::BTreeSet;

use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;

/// One authored timestamped value; undefined observations carry their reason.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputPoint {
    /// Instrument identity.
    pub symbol: String,
    /// Source cadence; ticks use an explicitly declared output window.
    pub interval: String,
    /// Timestamp of the represented observation.
    pub event_time_ns: i64,
    /// Timestamp when the observation becomes usable.
    pub available_at_ns: i64,
    /// Finite value, or undefined.
    pub value: Option<f64>,
    /// Explanation required for an undefined value.
    pub undefined_reason: Option<String>,
}

/// An event whose retrospective location is distinct from its confirmation.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputEvent {
    /// Instrument identity.
    pub symbol: String,
    /// Event location in Unix nanoseconds.
    pub event_time_ns: i64,
    /// Causal confirmation time in Unix nanoseconds.
    pub available_at_ns: i64,
    /// Named event attributes, including direction, duration, and magnitude.
    pub fields: BTreeMap<String, Value>,
}

/// Explicit supported exploratory outputs, without file or network access.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum StudyOutput {
    /// Host-sealed replay observations. Authored publication of this variant
    /// is rejected; reference `causal/<dataset>` from views instead.
    CausalObservations {
        dataset: harness_contracts::causal::CausalObservations,
    },
    /// Semantic description of one output, independent of an exhibit.
    DatasetSemantics {
        semantics: crate::research::DatasetSemantics,
    },
    /// A declarative exhibit referencing other outputs in this program instance.
    ResearchView { view: crate::research::ResearchView },
    /// Frozen fitted state for separately authored inference, with provenance.
    FittedState {
        /// Explicit supported fitting method.
        method: String,
        /// Inclusive training window start.
        training_start_ns: i64,
        /// Exclusive training window end.
        training_end_ns: i64,
        /// Earliest availability of the complete fitted state.
        available_at_ns: i64,
        /// Declared random seed (zero for deterministic methods).
        seed: u64,
        /// Declared method configuration.
        parameters: BTreeMap<String, Value>,
        /// Frozen finite learned coefficients.
        weights: BTreeMap<String, f64>,
    },
    /// Authored sequences, including warm-up and missing states. Timing shape
    /// is validated; only `CausalObservations` has replay-sealed provenance.
    Series {
        /// Ordered observations.
        points: Vec<OutputPoint>,
    },
    /// Confirmed events with descriptive attributes.
    Events {
        /// Ordered events.
        events: Vec<OutputEvent>,
    },
    /// Rectangular table with named columns and JSON scalar cells.
    Table {
        /// Unique, nonempty column names.
        columns: Vec<String>,
        /// Rows in publication order.
        rows: Vec<Vec<Value>>,
    },
}

impl StudyOutput {
    /// Validate semantic shape before the host accepts immutable evidence.
    ///
    /// # Errors
    /// Rejects impossible timing, nonfinite values, unexplained undefined
    /// observations, duplicate columns, nested cells, and ragged tables.
    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::CausalObservations { dataset } => dataset.validate()?,
            Self::ResearchView { view } => view.validate()?,
            Self::DatasetSemantics { semantics } => semantics.validate()?,
            Self::FittedState {
                method,
                training_start_ns,
                training_end_ns,
                available_at_ns,
                weights,
                ..
            } => {
                if method.trim().is_empty() {
                    return Err("fitted_state.method must be nonempty".into());
                }
                if training_start_ns >= training_end_ns || training_end_ns > available_at_ns {
                    return Err(format!("fitted_state timing: require training_start_ns < training_end_ns <= available_at_ns; received {training_start_ns}, {training_end_ns}, {available_at_ns}"));
                }
                if weights.is_empty() {
                    return Err("fitted_state.weights must be nonempty".into());
                }
                for (name, value) in weights {
                    if !value.is_finite() {
                        return Err(format!("fitted_state.weights[{name:?}]={value:?}: expected a finite coefficient"));
                    }
                }
            }
            Self::Series { points } => {
                for (index, point) in points.iter().enumerate() {
                    if point.symbol.is_empty() || point.interval.is_empty() {
                        return Err(format!("series.points[{index}]: symbol={:?}, interval={:?}; both must be nonempty", point.symbol, point.interval));
                    }
                    if point.event_time_ns > point.available_at_ns {
                        return Err(format!(
                            "series.points[{index}]: event_time_ns={} exceeds available_at_ns={}",
                            point.event_time_ns, point.available_at_ns
                        ));
                    }
                    match (point.value, point.undefined_reason.as_deref()) {
                        (Some(value), None) if value.is_finite() => {}
                        (None, Some(reason)) if !reason.trim().is_empty() => {}
                        _ => return Err(format!("series.points[{index}]: value={:?}, undefined_reason={:?}; supply either a finite value without a reason, or null with a nonempty reason", point.value, point.undefined_reason)),
                    }
                }
            }
            Self::Events { events } => {
                for (index, event) in events.iter().enumerate() {
                    if event.symbol.is_empty() || event.event_time_ns > event.available_at_ns {
                        return Err(format!("events[{index}]: symbol={:?}, event_time_ns={}, available_at_ns={}; require a nonempty symbol and event_time_ns <= available_at_ns", event.symbol, event.event_time_ns, event.available_at_ns));
                    }
                }
            }
            Self::Table { columns, rows } => {
                if columns.is_empty() {
                    return Err("table.columns must be nonempty".into());
                }
                let mut unique = BTreeSet::new();
                for (index, column) in columns.iter().enumerate() {
                    if column.trim().is_empty() || !unique.insert(column) {
                        return Err(format!("table.columns[{index}]={column:?}: columns must have nonempty unique names"));
                    }
                }
                for (index, row) in rows.iter().enumerate() {
                    if row.len() != columns.len() {
                        return Err(format!("table.rows[{index}]: received {} cells; expected {} to match columns {:?}", row.len(), columns.len(), columns));
                    }
                    for (column, cell) in columns.iter().zip(row) {
                        if cell.is_array() || cell.is_object() {
                            return Err(format!("table.rows[{index}][{column:?}]: expected a JSON scalar or null, received a nested collection"));
                        }
                    }
                }
            }
        }
        Ok(())
    }
}

/// Publish every composed indicator's canonical recorded OHLC sequence.
/// Stable dependency paths follow the pinned program's declared indicator order.
pub(crate) fn publish_indicator_outputs<C: crate::MarketConsumer + ?Sized>(
    consumer: &mut C,
    publisher: &mut crate::MetricPublisher,
) -> Result<(), String> {
    fn visit<C: crate::MarketConsumer + ?Sized>(
        consumer: &mut C,
        publisher: &mut crate::MetricPublisher,
        prefix: &str,
        seen: &mut BTreeSet<(usize, &'static str)>,
    ) -> Result<(), String> {
        for (index, indicator) in consumer.indicators().into_iter().enumerate() {
            if !seen.insert(indicator.consumer_identity()) {
                continue;
            }
            let path = format!("{prefix}/{index}");
            visit(indicator, publisher, &path, seen)?;
            let sequence = indicator.sequence();
            let columns = [
                "symbol",
                "interval",
                "start_ns",
                "end_ns",
                "available_at_ns",
                "open",
                "high",
                "low",
                "close",
                "observations",
                "missing",
            ]
            .into_iter()
            .map(str::to_string)
            .collect::<Vec<_>>();
            let row = |symbol: &str, interval: &str, bar: &crate::IndicatorBar| {
                vec![
                    Value::from(symbol),
                    Value::from(interval),
                    Value::from(bar.start_ns),
                    Value::from(bar.end_ns),
                    bar.available_at_ns.map_or(Value::Null, Value::from),
                    bar.open
                        .map_or(Value::Null, |value| Value::from(value.to_string())),
                    bar.high
                        .map_or(Value::Null, |value| Value::from(value.to_string())),
                    bar.low
                        .map_or(Value::Null, |value| Value::from(value.to_string())),
                    bar.close
                        .map_or(Value::Null, |value| Value::from(value.to_string())),
                    Value::from(bar.observations),
                    bar.missing.map_or(Value::Null, |missing| {
                        Value::from(match missing {
                            crate::IndicatorMissing::MissingInput => "missing_input",
                            crate::IndicatorMissing::Undefined => "undefined",
                        })
                    }),
                ]
            };
            let rows = sequence
                .completed()
                .iter()
                .flat_map(|((symbol, interval), bars)| {
                    bars.iter().map(|bar| row(symbol, interval, bar))
                })
                .collect();
            publisher
                .publish_output(
                    &format!("{path}/completed"),
                    &StudyOutput::Table {
                        columns: columns.clone(),
                        rows,
                    },
                )
                .map_err(|error| error.to_string())?;
            let cadence = format!("ticks:{}", sequence.tick_interval_ns().unwrap_or(0));
            let rows = sequence
                .in_progress()
                .iter()
                .map(|(symbol, bar)| row(symbol, &cadence, bar))
                .collect();
            publisher
                .publish_output(
                    &format!("{path}/in_progress"),
                    &StudyOutput::Table { columns, rows },
                )
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }
    visit(consumer, publisher, "ind", &mut BTreeSet::new())
}
