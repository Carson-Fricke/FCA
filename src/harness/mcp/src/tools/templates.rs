//! Owns MCP templates behavior over the injected capabilities; no host composition.
use super::*;

/// Return the embedded starter backtest configuration document.
///
/// # Errors
///
/// Returns [`ToolExecutionError`] when the template cannot be serialized.
pub(super) fn backtest_config_template() -> Result<Value, ToolExecutionError> {
    Ok(json!({
        "format": "yaml",
        "template": BACKTEST_CONFIG_TEMPLATE,
    }))
}

/// Return the embedded starter screening configuration document.
///
/// The template is a complete `shell: screen` document with an inline
/// example program using `export_screening_program!`; its embedded
/// `bundle_hash` matches the example source, and an edited source requires
/// recomputing that hash.
///
/// # Errors
///
/// Returns [`ToolExecutionError`] when the template cannot be serialized.
pub(super) fn screener_config_template() -> Result<Value, ToolExecutionError> {
    Ok(json!({
        "format": "yaml",
        "template": SCREENER_CONFIG_TEMPLATE,
    }))
}
