//! Environment-override extraction matching pydantic-settings.
//!
//! Environment variables use the `LAKE_` prefix and `__` nesting delimiter,
//! are matched case-insensitively, and land above YAML defaults. Unknown
//! `LAKE_*` variables are ignored, exactly as pydantic-settings treats env
//! values that match no model field.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::error::ConfigError;

/// Case-insensitive environment lookup.
pub(crate) fn lookup(environ: &BTreeMap<String, String>, key: &str) -> Option<String> {
    if let Some(value) = environ.get(key) {
        return Some(value.clone());
    }
    let wanted = key.to_uppercase();
    environ
        .iter()
        .find(|(candidate, _)| candidate.to_uppercase() == wanted)
        .map(|(_, value)| value.clone())
}

/// One known environment-overridable setting path and its value kind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum EnvKind {
    /// A plain string (or null when empty and nullable).
    Str,
    /// A boolean (`true/false/1/0/yes/no/on/off`).
    Bool,
    /// A signed integer.
    Int,
    /// A non-negative integer.
    Uint,
    /// A floating-point number.
    Float,
    /// A JSON array or comma-separated list of strings.
    StrList,
    /// A JSON array or comma-separated list of integers.
    IntList,
}

/// Insert `value` at a dotted `a.b.c` path, creating missing objects.
pub(crate) fn set_path(document: &mut Value, path: &str, value: Value) {
    let segments: Vec<&str> = path.split('.').collect();
    let mut cursor = document;
    for segment in &segments[..segments.len() - 1] {
        if !cursor.is_object() {
            *cursor = Value::Object(Map::new());
        }
        let map = cursor.as_object_mut().expect("object just ensured");
        cursor = map
            .entry((*segment).to_string())
            .or_insert_with(|| Value::Object(Map::new()));
    }
    if !cursor.is_object() {
        *cursor = Value::Object(Map::new());
    }
    cursor
        .as_object_mut()
        .expect("object just ensured")
        .insert(segments[segments.len() - 1].to_string(), value);
}

fn parse_bool(text: &str) -> Option<bool> {
    match text.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

/// Apply every known `path` override from the environment onto `document`.
pub(crate) fn apply(
    document: &mut Value,
    environ: &BTreeMap<String, String>,
    schema: &[(&str, EnvKind)],
) -> Result<(), ConfigError> {
    for (path, kind) in schema {
        let env_name = format!("LAKE_{}", path.replace('.', "__").to_uppercase());
        let Some(raw) = lookup(environ, &env_name) else {
            continue;
        };
        let value = match kind {
            EnvKind::Str => Value::String(raw),
            EnvKind::Bool => Value::Bool(parse_bool(&raw).ok_or_else(|| {
                ConfigError::new(format!("{env_name} must be a boolean, got {raw:?}"))
            })?),
            EnvKind::Int => Value::Number(
                raw.trim()
                    .parse::<i64>()
                    .map_err(|_| {
                        ConfigError::new(format!("{env_name} must be an integer, got {raw:?}"))
                    })?
                    .into(),
            ),
            EnvKind::Uint => Value::Number(
                raw.trim()
                    .parse::<u64>()
                    .map_err(|_| {
                        ConfigError::new(format!(
                            "{env_name} must be a non-negative integer, got {raw:?}"
                        ))
                    })?
                    .into(),
            ),
            EnvKind::Float => Value::Number(
                serde_json::Number::from_f64(raw.trim().parse::<f64>().map_err(|_| {
                    ConfigError::new(format!("{env_name} must be a number, got {raw:?}"))
                })?)
                .ok_or_else(|| ConfigError::new(format!("{env_name} must be finite")))?,
            ),
            EnvKind::StrList => {
                let items = parse_list(&raw).map_err(|error| {
                    ConfigError::new(format!("{env_name} must be a list: {error}"))
                })?;
                Value::Array(items.into_iter().map(Value::String).collect())
            }
            EnvKind::IntList => {
                let items = parse_list(&raw).map_err(|error| {
                    ConfigError::new(format!("{env_name} must be a list: {error}"))
                })?;
                let mut values = Vec::with_capacity(items.len());
                for item in items {
                    values.push(Value::Number(
                        item.trim()
                            .parse::<i64>()
                            .map_err(|_| {
                                ConfigError::new(format!(
                                    "{env_name} entries must be integers, got {item:?}"
                                ))
                            })?
                            .into(),
                    ));
                }
                Value::Array(values)
            }
        };
        set_path(document, path, value);
    }
    Ok(())
}

fn parse_list(text: &str) -> Result<Vec<String>, String> {
    let trimmed = text.trim();
    if trimmed.starts_with('[') {
        let parsed: Value = serde_json::from_str(trimmed).map_err(|error| error.to_string())?;
        let array = parsed
            .as_array()
            .ok_or_else(|| "expected a JSON array".to_string())?;
        array
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| "expected string entries".to_string())
            })
            .collect()
    } else if trimmed.is_empty() {
        Ok(Vec::new())
    } else {
        Ok(trimmed
            .split(',')
            .map(|item| item.trim().to_string())
            .collect())
    }
}
