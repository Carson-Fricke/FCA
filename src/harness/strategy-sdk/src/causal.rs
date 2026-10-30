//! Callback-local capture. The host seals features now and resolves labels later.
//!
//! Implement `MarketConsumer::on_decision` to capture after dependencies and
//! `on_bar` have consumed the current frame. No timestamp, row-update, label-read,
//! or finalization method is exposed. Warm-up can skip capture. The same API is
//! used by studies and strategies. Only host outputs named `causal/<dataset>`
//! carry this guarantee; arbitrary `on_stop` tables remain exploratory.

use std::collections::BTreeMap;
use std::marker::PhantomData;

use harness_contracts::causal::CaptureRequest;

use crate::{CDecisionSink, CStr, SDK_OK};

/// A borrowed capability valid only for the current bar decision callback.
///
/// ```compile_fail
/// use harness_strategy_sdk::DecisionContext;
/// struct Retained { saved: Option<DecisionContext<'static>> }
/// impl Retained {
///     fn retain(&mut self, context: DecisionContext<'_>) {
///         self.saved = Some(context);
///     }
/// }
/// ```
pub struct DecisionContext<'a> {
    sink: &'a CDecisionSink,
    // Keep this capability local to the callback thread.
    _local: PhantomData<std::rc::Rc<()>>,
}

impl<'a> DecisionContext<'a> {
    pub(crate) fn new(sink: &'a CDecisionSink) -> Self {
        Self {
            sink,
            _local: PhantomData,
        }
    }

    /// Host availability clock, including the completed bar's close.
    pub fn decision_at_ns(&self) -> i64 {
        self.sink.decision_at_ns
    }

    /// Cadence of the current atomic frame.
    pub fn interval(&self) -> &str {
        // The host owns these bytes until this callback returns.
        unsafe { self.sink.interval.as_str() }.expect("host interval must be UTF-8")
    }

    /// Copy and seal 1..=256 finite features for a symbol present in this frame.
    /// Dataset and feature names contain 1..=64 ASCII letters, digits, '.', '_'
    /// or '-'. Pass `"signal"`, never `"causal/signal"`: the host adds `causal/`.
    /// Skip capture until every feature is defined and finite.
    ///
    /// ```no_run
    /// # use harness_strategy_sdk::DecisionContext;
    /// # use std::collections::BTreeMap;
    /// # fn observe(context: &mut DecisionContext<'_>) -> Result<(), String> {
    /// let features = BTreeMap::from([("momentum".into(), 0.02)]);
    /// context.capture("signal", "MSFT", features, 60_000_000_000)?;
    /// # Ok(()) }
    /// ```
    /// The positive elapsed horizon must match an exact future availability on
    /// this cadence. Missing prices and end-of-run horizons remain undefined.
    /// Dataset feature names and horizon cannot change after the first capture.
    /// Rejection fails the run even if the caller ignores this return value.
    pub fn capture(
        &mut self,
        dataset: &str,
        symbol: &str,
        features: BTreeMap<String, f64>,
        horizon_ns: i64,
    ) -> Result<(), String> {
        let json = crate::serde_json::to_string(&CaptureRequest {
            dataset: dataset.into(),
            symbol: symbol.into(),
            features,
            horizon_ns,
        })
        .map_err(|error| error.to_string())?;
        let result = unsafe { (self.sink.capture)(self.sink.ctx, CStr::from_slice(&json)) };
        if result == SDK_OK {
            Ok(())
        } else {
            Err(format!("capture dataset={dataset:?}, symbol={symbol:?}, horizon_ns={horizon_ns}: host rejected the request; inspect the job failure for the exact field and constraint"))
        }
    }
}
