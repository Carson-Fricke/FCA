//! Rust strategy binding over the host loader.
//!
//! Mirrors `harness.strategy.binding.StrategyBinding`: the binding owns one
//! loaded strategy and its context, ensures a context is present, registers at
//! most once, refuses a second start, treats a stop before start as a no-op,
//! and routes typed events to their callbacks.

use crate::contracts::{callback_name_for_event, ContractError};
use crate::strategy::LoadedStrategy;

/// One strategy instance connected to the typed event callbacks.
pub struct StrategyBinding {
    strategy: LoadedStrategy,
    context_json: String,
    attached: bool,
    started: bool,
}

impl StrategyBinding {
    /// Bind a loaded strategy to its serialized context.
    ///
    /// # Errors
    ///
    /// Returns a `ValueError`-equivalent when no context event bus is present.
    pub fn new(
        strategy: LoadedStrategy,
        context_json: impl Into<String>,
    ) -> Result<Self, ContractError> {
        let context_json = context_json.into();
        if context_json.trim().is_empty() {
            return Err(ContractError::value_error(
                "strategy binding requires a context event bus",
            ));
        }
        Ok(Self {
            strategy,
            context_json,
            attached: false,
            started: false,
        })
    }

    /// The bound strategy.
    #[must_use]
    pub fn strategy(&self) -> &LoadedStrategy {
        &self.strategy
    }

    /// Whether `on_start` has been invoked.
    #[must_use]
    pub fn started(&self) -> bool {
        self.started
    }

    /// Register the binding once; this does not start callbacks.
    pub fn attach(&mut self) {
        self.attached = true;
    }

    /// Whether the binding has been registered.
    #[must_use]
    pub fn attached(&self) -> bool {
        self.attached
    }

    /// Attach and invoke the strategy's start callback.
    ///
    /// # Errors
    ///
    /// Returns a `RuntimeError`-equivalent on a second start, or the
    /// strategy's own error when `on_start` raises.
    pub fn start(&mut self) -> Result<(), ContractError> {
        if self.started {
            return Err(ContractError::runtime_error(
                "strategy binding is already started",
            ));
        }
        self.attach();
        self.started = true;
        self.strategy
            .call("on_start", Some(&self.context_json))
            .map_err(|error| ContractError::value_error(error.to_string()))
    }

    /// Invoke the strategy's stop callback once.
    ///
    /// # Errors
    ///
    /// Returns the strategy's own error when `on_stop` raises.
    pub fn stop(&mut self) -> Result<(), ContractError> {
        if !self.started {
            return Ok(());
        }
        self.started = false;
        self.strategy
            .call("on_stop", Some(&self.context_json))
            .map_err(|error| ContractError::value_error(error.to_string()))
    }

    /// Route one typed event to its callback, returning whether it was
    /// delivered.
    ///
    /// # Errors
    ///
    /// Returns the strategy's own error when the callback raises.
    pub fn receive(&self, event_kind: &str, payload_json: &str) -> Result<bool, ContractError> {
        if !self.started {
            return Ok(false);
        }
        let callback = callback_name_for_event(event_kind);
        if self
            .strategy
            .has_callback(callback)
            .map_err(|error| ContractError::value_error(error.to_string()))?
        {
            self.strategy
                .call(callback, Some(payload_json))
                .map_err(|error| ContractError::value_error(error.to_string()))?;
        }
        Ok(true)
    }
}
