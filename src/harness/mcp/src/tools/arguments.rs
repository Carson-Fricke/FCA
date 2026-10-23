//! Owns MCP argument validation and configuration materialization before dispatch.
use super::*;
pub(super) fn remote_unconfigured() -> ToolExecutionError {
    ToolExecutionError::new("the remote harness API boundary is not configured for this MCP server")
        .with_certainty("not_started")
}

pub(super) fn credential_rejected(status_code: u16) -> ToolExecutionError {
    ToolExecutionError::new(format!(
        "the remote harness API rejected the bearer credential (HTTP {status_code})"
    ))
    .with_certainty("not_started")
}

pub(super) fn remote_response_failure(
    response: &RemoteResponse,
    message: &str,
) -> ToolExecutionError {
    let certainty = response
        .body
        .get("executionCertainty")
        .or_else(|| {
            response
                .body
                .get("error")
                .and_then(|error| error.get("executionCertainty"))
        })
        .and_then(Value::as_str)
        .filter(|value| {
            matches!(
                *value,
                "not_started" | "observed_failure" | "unknown_outcome"
            )
        })
        .unwrap_or(
            if (400..500).contains(&response.status_code) || response.status_code == 501 {
                "not_started"
            } else {
                "unknown_outcome"
            },
        );
    let detail = response
        .body
        .get("error")
        .and_then(|error| error.get("message"))
        .or_else(|| response.body.get("message"))
        .and_then(Value::as_str);
    ToolExecutionError::with_payload(
        format!("{message} (HTTP {}){}", response.status_code, detail.map(|detail| format!(": {detail}")).unwrap_or_default()),
        json!({"boundary": "remote", "statusCode": response.status_code, "serverError": response.body}),
    ).with_certainty(certainty)
}

pub(super) fn text_argument(
    arguments: &Map<String, Value>,
    key: &str,
) -> Result<String, ToolExecutionError> {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| ToolExecutionError::new(format!("{key} must be a non-empty string")))
}

pub(super) fn identifier_argument(
    arguments: &Map<String, Value>,
    key: &str,
) -> Result<String, ToolExecutionError> {
    let value = text_argument(arguments, key)?;
    if value.len() > IDENTIFIER_MAX {
        return Err(ToolExecutionError::new(format!(
            "{key} must be at most {IDENTIFIER_MAX} characters"
        )));
    }
    Ok(value)
}

pub(super) fn optional_identifier_argument(
    arguments: &Map<String, Value>,
    key: &str,
) -> Result<Option<String>, ToolExecutionError> {
    match arguments.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => identifier_argument(arguments, key).map(Some),
    }
}

/// Resolve the backtest configuration path from an existing path or an inline
/// ``config`` document.
///
/// An inline document is serialized and written to a content-addressed file
/// under the temp directory so the asynchronous runner can read it after the
/// tool call returns; identical documents reuse the same file.
pub(super) fn resolve_config(
    arguments: &Map<String, Value>,
    job_id: Option<&str>,
) -> Result<String, ToolExecutionError> {
    if let Some(value) = arguments.get("config") {
        if !value.is_null() {
            let Some(document) = value.as_object() else {
                return Err(ToolExecutionError::new("config must be a JSON object"));
            };
            let mut document = Value::Object(document.clone());
            harness_research::configuration::overlay_job_id_json(&mut document, job_id)
                .map_err(|error| ToolExecutionError::new(error.to_string()))?;
            validate_configuration(&document, None)?;
            let text = serde_json::to_string(&document)
                .map_err(|error| ToolExecutionError::new(error.to_string()))?;
            return materialize_config(&text);
        }
    }
    text_argument(arguments, "config_path")
}

/// Validate with the same replay and screening schemas used by API and engine.
pub fn validate_configuration(
    document: &Value,
    base: Option<&std::path::Path>,
) -> Result<(), ToolExecutionError> {
    if document.get("shell").and_then(Value::as_str) == Some("screen") {
        backtest::backtest::screen::ScreenConfig::from_json_value_with_base(document, base)
            .map_err(|e| ToolExecutionError::new(e.to_string()))?;
    } else {
        backtest::backtest::config::BacktestConfig::from_yaml(&document.to_string())
            .map_err(|e| ToolExecutionError::new(e.to_string()))?;
    }
    Ok(())
}

pub(super) fn materialize_config(text: &str) -> Result<String, ToolExecutionError> {
    let digest = hex::encode(Sha256::digest(text.as_bytes()));
    let directory = std::env::temp_dir().join("harness-mcp-configs");
    std::fs::create_dir_all(&directory).map_err(|error| {
        ToolExecutionError::new(format!("could not prepare the config directory: {error}"))
    })?;
    let path = directory.join(format!("{digest}.json"));
    if !path.exists() {
        std::fs::write(&path, text).map_err(|error| {
            ToolExecutionError::new(format!(
                "could not write the inline backtest config: {error}"
            ))
        })?;
    }
    Ok(path.to_string_lossy().into_owned())
}

pub(super) fn workspace_option(
    arguments: &Map<String, Value>,
) -> Result<Option<String>, ToolExecutionError> {
    match arguments.get("workspace") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(workspace)) if !workspace.trim().is_empty() => {
            Ok(Some(workspace.clone()))
        }
        Some(_) => Err(ToolExecutionError::new(
            "workspace must be a non-empty string",
        )),
    }
}

/// File adapter for the shared strict JSON/YAML submitted-document parser.
#[derive(Debug, Clone, Copy, Default)]
pub struct SharedFileConfigurationLoader;

impl ConfigurationLoader for SharedFileConfigurationLoader {
    fn load_json(&self, path: &str) -> Result<Value, String> {
        let text = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
        config::replay::submitted_document(&text).map_err(|error| error.to_string())
    }
}
