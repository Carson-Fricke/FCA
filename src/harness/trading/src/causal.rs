//! Replay-owned immutable feature capture and delayed, exact-horizon labels.

use std::collections::{BTreeMap, BTreeSet};

use harness_contracts::causal::{
    valid_capture_name, CaptureRequest, CausalObservations, OBSERVATION_COLUMNS,
};
use num::Decimal;
use serde_json::Value;

use crate::program::EngineBarFrame;

struct Observation {
    id: u64,
    symbol: String,
    interval: String,
    decision: i64,
    due: i64,
    reference: Decimal,
    features: Vec<f64>,
    outcome: Option<(i64, Option<(Decimal, f64)>)>,
}

struct Dataset {
    features: Vec<String>,
    horizon: i64,
    rows: Vec<Observation>,
}

/// Private rows never cross back into authored execution. Only the active
/// callback may append; final publication offers no mutation or backdating API.
#[derive(Default)]
pub struct ObservationLedger {
    datasets: BTreeMap<String, Dataset>,
    last_clock: Option<i64>,
    frames: BTreeMap<String, i64>,
    captured: BTreeSet<(String, String)>,
    pending: BTreeMap<String, BTreeMap<i64, Vec<(String, usize)>>>,
    next_id: u64,
}

impl ObservationLedger {
    /// Advance outcomes before entering the current frame's authored callbacks.
    pub fn advance(&mut self, frame: &EngineBarFrame) -> Result<(), String> {
        if frame.interval_start_ns >= frame.interval_end_ns
            || frame.interval_end_ns > frame.available_at_ns
            || self
                .last_clock
                .is_some_and(|clock| frame.available_at_ns < clock)
            || self
                .frames
                .get(&frame.interval)
                .is_some_and(|clock| frame.available_at_ns <= *clock)
        {
            return Err(
                "causal replay requires ordered, nonduplicate frames with valid availability"
                    .into(),
            );
        }
        self.last_clock = Some(frame.available_at_ns);
        self.frames
            .insert(frame.interval.clone(), frame.available_at_ns);
        self.captured.clear();
        if let Some(pending) = self.pending.get_mut(&frame.interval) {
            while pending
                .first_key_value()
                .is_some_and(|(due, _)| *due <= frame.available_at_ns)
            {
                let (_, rows) = pending.pop_first().expect("pending horizon");
                for (dataset, index) in rows {
                    let row = &mut self
                        .datasets
                        .get_mut(&dataset)
                        .expect("captured dataset")
                        .rows[index];
                    let outcome = if row.due == frame.available_at_ns {
                        frame
                            .bars_by_symbol
                            .get(&row.symbol)
                            .map(|bar| {
                                let value = bar
                                    .close
                                    .checked_div(row.reference)
                                    .and_then(|ratio| ratio.checked_sub(Decimal::ONE))
                                    .and_then(|change| change.checked_mul(Decimal::from(10_000)))
                                    .and_then(Decimal::to_f64_checked)
                                    .map_err(|error| {
                                        format!("causal outcome arithmetic failed: {error}")
                                    })?;
                                Ok::<_, String>((bar.close, value))
                            })
                            .transpose()?
                    } else {
                        None
                    };
                    row.outcome = Some((frame.available_at_ns, outcome));
                }
            }
        }
        Ok(())
    }

    /// Seal a copied feature vector against the actual host frame, never a
    /// caller-provided clock. The adapter owns this frame and callback lifetime.
    pub fn capture(
        &mut self,
        frame: &EngineBarFrame,
        request: CaptureRequest,
    ) -> Result<(), String> {
        if self.last_clock != Some(frame.available_at_ns)
            || self.frames.get(&frame.interval) != Some(&frame.available_at_ns)
        {
            return Err(format!(
                "capture.frame: no active {} frame at {}; ledger clock is {:?}",
                frame.interval, frame.available_at_ns, self.last_clock
            ));
        }
        if !valid_capture_name(&request.dataset) {
            return Err(format!("capture.dataset: invalid name {:?}; expected 1..=64 ASCII letters, digits, '.', '_' or '-'. Pass an unprefixed name; the host publishes causal/<dataset>", request.dataset));
        }
        if request.features.is_empty() || request.features.len() > 256 {
            return Err(format!(
                "capture.features: received {} features; expected 1..=256",
                request.features.len()
            ));
        }
        for (name, value) in &request.features {
            if !valid_capture_name(name) {
                return Err(format!("capture.features[{name:?}]: invalid name; expected 1..=64 ASCII letters, digits, '.', '_' or '-'"));
            }
            if !value.is_finite() {
                return Err(format!("capture.features[{name:?}]: received {value:?}; expected a finite value; skip capture during warm-up"));
            }
        }
        if request.horizon_ns <= 0 {
            return Err(format!("capture.horizon_ns: received {}; expected a positive elapsed horizon in nanoseconds", request.horizon_ns));
        }
        let due = frame
            .available_at_ns
            .checked_add(request.horizon_ns)
            .ok_or_else(|| {
                format!(
                    "capture.horizon_ns={}: adding to decision_at_ns={} overflows i64 nanoseconds",
                    request.horizon_ns, frame.available_at_ns
                )
            })?;
        let bar = frame
            .bars_by_symbol
            .get(&request.symbol)
            .ok_or_else(|| format!("capture.symbol: {:?} is absent from the current {} frame at {}; available symbols: {:?}", request.symbol, frame.interval, frame.available_at_ns, frame.bars_by_symbol.keys().collect::<Vec<_>>()))?;
        if bar.close <= Decimal::ZERO {
            return Err(format!(
                "capture.reference_close={}: expected a positive price",
                bar.close
            ));
        }
        let features: Vec<_> = request.features.keys().cloned().collect();
        let dataset = self
            .datasets
            .entry(request.dataset.clone())
            .or_insert_with(|| Dataset {
                features: features.clone(),
                horizon: request.horizon_ns,
                rows: Vec::new(),
            });
        if dataset.features != features || dataset.horizon != request.horizon_ns {
            return Err(format!("capture.dataset {:?}: schema/horizon changed; expected features {:?} and horizon_ns {}, received {:?} and {}; use a distinct dataset for a distinct schema or horizon", request.dataset, dataset.features, dataset.horizon, features, request.horizon_ns));
        }
        if !self
            .captured
            .insert((request.dataset.clone(), request.symbol.clone()))
        {
            return Err(format!("capture.dataset {:?}, symbol {:?}: duplicate capture in one {} frame at {}; capture at most once per dataset/symbol/frame", request.dataset, request.symbol, frame.interval, frame.available_at_ns));
        }
        self.next_id += 1;
        self.pending
            .entry(frame.interval.clone())
            .or_default()
            .entry(due)
            .or_default()
            .push((request.dataset, dataset.rows.len()));
        dataset.rows.push(Observation {
            id: self.next_id,
            symbol: request.symbol,
            interval: frame.interval.clone(),
            decision: frame.available_at_ns,
            due,
            reference: bar.close,
            features: request.features.into_values().collect(),
            outcome: None,
        });
        Ok(())
    }

    /// Produce host evidence after replay. Unmatured rows stay explicitly
    /// unresolved; gaps never use a later price or a fabricated terminal fill.
    pub fn outputs(&self) -> BTreeMap<String, CausalObservations> {
        self.datasets
            .iter()
            .map(|(name, dataset)| {
                let feature_columns: Vec<_> = dataset
                    .features
                    .iter()
                    .map(|name| format!("feature/{name}"))
                    .collect();
                let columns = OBSERVATION_COLUMNS
                    .into_iter()
                    .map(str::to_string)
                    .chain(feature_columns.iter().cloned())
                    .collect();
                let rows = dataset
                    .rows
                    .iter()
                    .map(|row| {
                        let (available, close, value, status) = match row.outcome {
                            Some((at, Some((close, value)))) => (
                                Value::from(at.to_string()),
                                Value::from(close.to_string()),
                                Value::from(value),
                                "observed",
                            ),
                            Some((at, None)) => (
                                Value::from(at.to_string()),
                                Value::Null,
                                Value::Null,
                                "missing",
                            ),
                            None => (Value::Null, Value::Null, Value::Null, "end_of_run"),
                        };
                        let mut cells = vec![
                            Value::from(row.id.to_string()),
                            Value::from(row.symbol.clone()),
                            Value::from(row.interval.clone()),
                            Value::from(row.decision.to_string()),
                            Value::from(row.due.to_string()),
                            available,
                            Value::from(row.reference.to_string()),
                            close,
                            value,
                            Value::from(status),
                        ];
                        cells.extend(row.features.iter().copied().map(Value::from));
                        cells
                    })
                    .collect();
                (
                    format!("causal/{name}"),
                    CausalObservations {
                        horizon_ns: dataset.horizon.to_string(),
                        feature_columns,
                        columns,
                        rows,
                    },
                )
            })
            .collect()
    }
}
