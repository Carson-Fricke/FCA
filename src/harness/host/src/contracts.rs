//! Rust strategy contract enforcement.
//!
//! Mirrors `harness.strategy.contracts` and the rejection behavior of
//! `harness.strategy.binding`: the documented callback order is a fixed
//! constant, unknown callbacks are rejected, and lifecycle misuse raises the
//! same kind of error Python raises.

use std::collections::BTreeSet;
use std::fmt;

/// The documented callback dispatch order.
pub const CALLBACK_ORDER: [&str; 11] = [
    "on_start",
    "on_quote",
    "on_trade",
    "on_order",
    "on_execution_report",
    "on_fill",
    "on_position",
    "on_book",
    "on_account",
    "on_corporate_action",
    "on_stop",
];

/// The error kind, matching Python's exception class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContractErrorKind {
    /// A `TypeError`.
    Type,
    /// A `ValueError`.
    Value,
    /// A `RuntimeError`.
    Runtime,
}

/// A contract violation with the Python-equivalent exception kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractError {
    /// The Python exception this maps to.
    pub kind: ContractErrorKind,
    /// Human-readable message.
    pub message: String,
}

impl ContractError {
    /// A `TypeError` equivalent.
    #[must_use]
    pub fn type_error(message: impl Into<String>) -> Self {
        Self {
            kind: ContractErrorKind::Type,
            message: message.into(),
        }
    }

    /// A `ValueError` equivalent.
    #[must_use]
    pub fn value_error(message: impl Into<String>) -> Self {
        Self {
            kind: ContractErrorKind::Value,
            message: message.into(),
        }
    }

    /// A `RuntimeError` equivalent.
    #[must_use]
    pub fn runtime_error(message: impl Into<String>) -> Self {
        Self {
            kind: ContractErrorKind::Runtime,
            message: message.into(),
        }
    }
}

impl fmt::Display for ContractError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ContractError {}

/// Map an event kind to its callback name, mirroring `_callback_name`.
#[must_use]
pub fn callback_name_for_event(event_kind: &str) -> &'static str {
    match event_kind {
        "quote" => "on_quote",
        "trade" => "on_trade",
        "order" => "on_order",
        "execution_report" => "on_execution_report",
        "fill" => "on_fill",
        "position" => "on_position",
        "book" => "on_book",
        "account" => "on_account",
        "corporate_action" => "on_corporate_action",
        "start" => "on_start",
        "stop" => "on_stop",
        _ => "on_event",
    }
}

/// Return whether a name is one of the documented callbacks.
#[must_use]
pub fn is_known_callback(name: &str) -> bool {
    CALLBACK_ORDER.contains(&name)
}

/// Validate a supplied callback order against the documented contract.
///
/// # Errors
///
/// Returns a [`ContractError`] when the order differs from [`CALLBACK_ORDER`].
pub fn validate_callback_order(order: &[&str]) -> Result<(), ContractError> {
    if order == CALLBACK_ORDER {
        return Ok(());
    }
    Err(ContractError::value_error(format!(
        "strategy callback order must match the documented contract: {CALLBACK_ORDER:?}"
    )))
}

/// Require every listed callback to be present on a strategy.
///
/// # Errors
///
/// Returns a [`ContractError`] naming the first missing callback.
pub fn require_callbacks(
    present: &BTreeSet<String>,
    required: &[&str],
) -> Result<(), ContractError> {
    for callback in required {
        if !present.contains(*callback) {
            return Err(ContractError::type_error(format!(
                "strategy must define {callback}"
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "../tests/unit/contracts.rs"]
mod tests;
