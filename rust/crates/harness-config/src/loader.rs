//! Safe configuration loading and runtime secret resolution.
//!
//! Ports `harness.configuration.loader`: the YAML-subset parser, committed
//! secret rejection, strict model validation, and `${ENV_VAR}` resolution.

use std::collections::BTreeMap;

use crate::models::{build_api, build_backtest, build_trader};
use crate::yaml::{load_yaml_mapping, YamlValue};
use crate::{ConfigError, HarnessConfig};

fn is_environment_reference(value: &str) -> bool {
    let inner = value
        .strip_prefix("${")
        .and_then(|text| text.strip_suffix('}'));
    let Some(inner) = inner else {
        return false;
    };
    let mut chars = inner.chars();
    let first = chars.next().unwrap_or(' ');
    (first.is_ascii_uppercase() || first == '_')
        && chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
}

fn is_reference_tree(value: &YamlValue) -> bool {
    match value {
        YamlValue::Str(text) => is_environment_reference(text),
        YamlValue::Map(map) => !map.is_empty() && map.values().all(is_reference_tree),
        YamlValue::Seq(items) => !items.is_empty() && items.iter().all(is_reference_tree),
        _ => false,
    }
}

fn secret_like_key(key: &str) -> bool {
    let normalized = key.to_ascii_lowercase();
    let needles = [
        "access_key",
        "accesskey",
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "bearer",
        "client_secret",
        "clientsecret",
        "connection_string",
        "connectionstring",
        "credential",
        "password",
        "private_key",
        "privatekey",
        "secret",
        "token",
    ];
    needles.iter().any(|needle| normalized.contains(needle))
}

fn reject_committed_secrets(value: &YamlValue, path: &str) -> Result<(), ConfigError> {
    match value {
        YamlValue::Map(map) => {
            for (key, item) in map {
                let item_path = if path.is_empty() {
                    key.clone()
                } else {
                    format!("{path}.{key}")
                };
                if secret_like_key(key) && !is_reference_tree(item) {
                    return Err(ConfigError(format!(
                        "{item_path} must contain only ${{ENV_VAR}} references"
                    )));
                }
                reject_committed_secrets(item, &item_path)?;
            }
        }
        YamlValue::Seq(items) => {
            for (index, item) in items.iter().enumerate() {
                reject_committed_secrets(item, &format!("{path}.{index}"))?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// Load and strictly validate one API, backtest, or trader document.
///
/// # Errors
///
/// Returns [`ConfigError`] for unreadable, malformed, or invalid documents.
pub fn load_configuration(path: &std::path::Path) -> Result<HarnessConfig, ConfigError> {
    let mapping = load_yaml_mapping(path)?;
    reject_committed_secrets(&YamlValue::Map(mapping.clone()), "")?;
    let shell = match mapping.get("shell") {
        Some(YamlValue::Str(shell)) => shell.clone(),
        Some(other) => {
            return Err(ConfigError(format!(
                "shell must be a string, got {}",
                match other {
                    YamlValue::Null => "null",
                    YamlValue::Bool(_) => "a boolean",
                    YamlValue::Int(_) => "an integer",
                    YamlValue::Decimal(_) => "a decimal",
                    YamlValue::Seq(_) => "a sequence",
                    YamlValue::Map(_) => "a mapping",
                    YamlValue::Str(_) => "a string",
                }
            )))
        }
        None => return Err(ConfigError("configuration has no 'shell' key".to_string())),
    };
    match shell.as_str() {
        "api" => build_api(&mapping).map(HarnessConfig::Api),
        "backtest" => build_backtest(&mapping).map(HarnessConfig::Backtest),
        "trader" => build_trader(&mapping).map(HarnessConfig::Trader),
        other => Err(ConfigError(format!(
            "configuration shell must be one of 'api', 'backtest', 'trader', not '{other}'"
        ))),
    }
}

/// Resolve `config/harness` without retaining a machine-specific path.
#[must_use]
pub fn harness_config_dir(root: Option<&std::path::Path>) -> std::path::PathBuf {
    let root_path = match root {
        Some(root) => root.to_path_buf(),
        None => match std::env::var("HARNESS_CONFIG_ROOT") {
            Ok(configured) => std::path::PathBuf::from(configured),
            Err(_) => std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("..")
                .join(".."),
        },
    };
    if root_path.file_name().and_then(|name| name.to_str()) == Some("harness")
        && root_path
            .parent()
            .and_then(|parent| parent.file_name())
            .and_then(|name| name.to_str())
            == Some("config")
    {
        return root_path;
    }
    root_path.join("config").join("harness")
}

/// Load the committed example for an execution shell.
///
/// # Errors
///
/// Returns [`ConfigError`] when the example is absent or invalid.
pub fn load_example(
    shell: &str,
    root: Option<&std::path::Path>,
) -> Result<HarnessConfig, ConfigError> {
    if !matches!(shell, "api" | "backtest" | "trader") {
        return Err(ConfigError(format!("unknown configuration shell: {shell}")));
    }
    let path = harness_config_dir(root).join(format!("{shell}.yaml"));
    load_configuration(&path)
}

/// Extract a validated environment-variable name from `${NAME}`.
///
/// # Errors
///
/// Returns [`ConfigError`] for any other syntax.
pub fn environment_variable_name(reference: &str) -> Result<String, ConfigError> {
    let inner = reference
        .strip_prefix("${")
        .and_then(|text| text.strip_suffix('}'));
    let Some(inner) = inner else {
        return Err(ConfigError(
            "environment reference must use ${ENV_VAR} syntax".to_string(),
        ));
    };
    let mut chars = inner.chars();
    let first = chars.next().unwrap_or(' ');
    if !(first.is_ascii_uppercase() || first == '_')
        || !chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
    {
        return Err(ConfigError(
            "environment reference must use ${ENV_VAR} syntax".to_string(),
        ));
    }
    Ok(inner.to_string())
}

/// Resolve one reference at runtime without placing its value in config.
///
/// # Errors
///
/// Returns [`ConfigError`] when the variable is missing or empty.
pub fn resolve_environment_reference(
    reference: &str,
    environ: Option<&BTreeMap<String, String>>,
) -> Result<String, ConfigError> {
    let variable = environment_variable_name(reference)?;
    let value = match environ {
        Some(environ) => environ.get(&variable).cloned(),
        None => std::env::var(&variable).ok(),
    };
    let Some(value) = value else {
        return Err(ConfigError(format!(
            "runtime environment variable {variable} is not set"
        )));
    };
    if value.is_empty() {
        return Err(ConfigError(format!(
            "runtime environment variable {variable} must not be empty"
        )));
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_references_and_names() {
        assert_eq!(
            environment_variable_name("${HARNESS_API_BEARER_TOKEN}").expect("name"),
            "HARNESS_API_BEARER_TOKEN"
        );
        assert!(environment_variable_name("HARNESS_API_BEARER_TOKEN").is_err());
        let mut environ = BTreeMap::new();
        environ.insert("A".to_string(), "value".to_string());
        assert_eq!(
            resolve_environment_reference("${A}", Some(&environ)).expect("resolve"),
            "value"
        );
        assert!(resolve_environment_reference("${B}", Some(&environ)).is_err());
    }

    #[test]
    fn rejects_committed_secret_values() {
        let text = "shell: api\npassword: hunter2\n";
        let value = crate::yaml::parse_yaml_mapping(text)
            .map(YamlValue::Map)
            .expect("yaml");
        assert!(reject_committed_secrets(&value, "").is_err());
        let safe = "shell: api\npassword: \"${PASSWORD}\"\n";
        let value = crate::yaml::parse_yaml_mapping(safe)
            .map(YamlValue::Map)
            .expect("yaml");
        assert!(reject_committed_secrets(&value, "").is_ok());
    }
}
