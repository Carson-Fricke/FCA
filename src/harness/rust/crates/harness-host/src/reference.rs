//! Rust port of the reference strategy callback recorder.
//!
//! `harness.strategy.reference.ReferenceStrategy` records every callback it
//! receives in the contract's documented order. This port preserves that
//! observable behavior without a Python interpreter, so the reference fixture
//! is available to the Rust runtime and parity suites.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::contracts::CALLBACK_ORDER;

/// A callback-recording strategy used as a public contract fixture.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReferenceStrategy {
    parameters: BTreeMap<String, Value>,
    callbacks: Vec<String>,
    events: Vec<String>,
}

impl ReferenceStrategy {
    /// Construct the recorder with constructor parameters.
    #[must_use]
    pub fn new(parameters: BTreeMap<String, Value>) -> Self {
        Self {
            parameters,
            callbacks: Vec::new(),
            events: Vec::new(),
        }
    }

    /// The documented callback order.
    #[must_use]
    pub fn callback_order() -> &'static [&'static str] {
        &CALLBACK_ORDER
    }

    /// The constructor parameters.
    #[must_use]
    pub fn parameters(&self) -> &BTreeMap<String, Value> {
        &self.parameters
    }

    /// Every callback name recorded, in dispatch order.
    #[must_use]
    pub fn callbacks(&self) -> &[String] {
        &self.callbacks
    }

    /// Every event payload recorded, in dispatch order.
    #[must_use]
    pub fn events(&self) -> &[String] {
        &self.events
    }

    /// Record one callback dispatch.
    pub fn dispatch(&mut self, callback: &str, payload_json: &str) {
        self.callbacks.push(callback.to_string());
        self.events.push(payload_json.to_string());
    }

    /// Record a lifecycle callback with no event payload.
    pub fn dispatch_lifecycle(&mut self, callback: &str) {
        self.callbacks.push(callback.to_string());
    }

    /// The recorded callbacks encoded as JSON for parity comparisons.
    #[must_use]
    pub fn callbacks_json(&self) -> Value {
        Value::Array(
            self.callbacks
                .iter()
                .map(|name| Value::String(name.clone()))
                .collect(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn records_callbacks_in_dispatch_order() {
        let mut strategy =
            ReferenceStrategy::new(BTreeMap::from([("label".to_string(), json!("alpha"))]));
        assert_eq!(strategy.parameters().get("label"), Some(&json!("alpha")));
        strategy.dispatch_lifecycle("on_start");
        strategy.dispatch("on_quote", r#"{"symbol":"SPY"}"#);
        strategy.dispatch("on_trade", r#"{"symbol":"SPY"}"#);
        strategy.dispatch_lifecycle("on_stop");
        assert_eq!(
            strategy.callbacks(),
            ["on_start", "on_quote", "on_trade", "on_stop"]
        );
        assert_eq!(strategy.events().len(), 2);
        assert_eq!(
            strategy.callbacks_json(),
            json!(["on_start", "on_quote", "on_trade", "on_stop"])
        );
    }
}
