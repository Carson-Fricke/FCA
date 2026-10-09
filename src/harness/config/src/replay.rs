//! Authoritative replay document validation. Compact shipped documents use explicit defaults.

use crate::{ConfigError, YamlValue};
use serde_json::Value;

/// Decode the submitted document without resolving defaults or changing its identity.
pub fn submitted_document(text: &str) -> Result<Value, ConfigError> {
    fn convert(value: YamlValue) -> Result<Value, ConfigError> {
        Ok(match value {
            YamlValue::Null => Value::Null,
            YamlValue::Bool(value) => Value::Bool(value),
            YamlValue::Int(value) => Value::from(value),
            YamlValue::Decimal(value) => {
                serde_json::from_str(&value).map_err(|error| ConfigError(error.to_string()))?
            }
            YamlValue::Str(value) => Value::String(value),
            YamlValue::Seq(values) => {
                Value::Array(values.into_iter().map(convert).collect::<Result<_, _>>()?)
            }
            YamlValue::Map(values) => Value::Object(
                values
                    .into_iter()
                    .map(|(key, value)| Ok((key, convert(value)?)))
                    .collect::<Result<_, ConfigError>>()?,
            ),
        })
    }
    // JSON retains number types exactly; YAML uses the existing safe subset parser.
    if let Ok(value) = serde_json::from_str::<Value>(text) {
        return Ok(value);
    }
    convert(YamlValue::Map(parse_document(text)?))
}
use std::collections::BTreeMap;

pub fn json_to_yaml(value: &Value) -> Result<YamlValue, ConfigError> {
    Ok(match value {
        Value::Null => YamlValue::Null,
        Value::Bool(v) => YamlValue::Bool(*v),
        Value::Number(v) => match v.as_i64() {
            Some(v) => YamlValue::Int(v),
            None if v.is_f64() => YamlValue::Decimal(v.to_string()),
            None => {
                return Err(ConfigError(
                    "integer outside supported signed 64-bit range".into(),
                ))
            }
        },
        Value::String(v) => YamlValue::Str(v.clone()),
        Value::Array(v) => YamlValue::Seq(v.iter().map(json_to_yaml).collect::<Result<_, _>>()?),
        Value::Object(v) => YamlValue::Map(
            v.iter()
                .map(|(k, v)| Ok((k.clone(), json_to_yaml(v)?)))
                .collect::<Result<_, ConfigError>>()?,
        ),
    })
}

/// One syntax parser for CLI, MCP, and engine documents. JSON numbers retain decimal identity.
pub fn parse_document(text: &str) -> Result<BTreeMap<String, YamlValue>, ConfigError> {
    if text.trim_start().starts_with('{') {
        let value: Value = serde_json::from_str(text).map_err(|e| ConfigError(e.to_string()))?;
        match json_to_yaml(&value)? {
            YamlValue::Map(map) => Ok(map),
            _ => Err(ConfigError("configuration root must be a mapping".into())),
        }
    } else {
        crate::parse_yaml_mapping(text)
    }
}

/// Resolve only documented, shipped replay defaults before strict field validation.
pub(crate) fn with_defaults(
    mapping: &BTreeMap<String, YamlValue>,
) -> Result<BTreeMap<String, YamlValue>, ConfigError> {
    let mut map = mapping.clone();
    let defaults = serde_json::json!({
        "schema_version":1, "start":"2024-01-01T00:00:00Z", "end":"2024-01-03T00:00:00Z",
        "clock":"event_time", "event_ordering":"timestamp_then_sequence_then_class_then_feed",
        "seed":0, "base_currency":"USD", "initial_cash":"1000000",
        "strategy_instances":[], "risk_pipeline":{"on_reject":"reject_order","rules":[]},
        "fees":{"currency":"USD","rounding":"half_even","rules":[]},
        "deployment":{"name":"backtest-job-local","target":"local","environment":"local","image":null,
            "replicas":1,"restart_policy":"never","network_access":"disabled","resources":{"cpu_cores":1,"memory_mib":1024}}
    });
    let YamlValue::Map(defaults) = json_to_yaml(&defaults)? else {
        unreachable!()
    };
    for (key, value) in defaults {
        if let Some(YamlValue::Map(entries)) = map.get_mut(&key) {
            if let YamlValue::Map(defaults) = value {
                for (key, value) in defaults {
                    entries.entry(key).or_insert(value);
                }
            }
        } else {
            map.entry(key).or_insert(value);
        }
    }
    if map.contains_key("symbols") {
        return Err(ConfigError(
            "top-level symbols is not accepted; declare data.symbols".into(),
        ));
    }
    let shell = match map.get("shell") {
        Some(YamlValue::Str(v)) => v.as_str(),
        _ => "",
    };
    if !matches!(shell, "backtest" | "study") {
        return Err(ConfigError(
            "configuration shell must be backtest or study".into(),
        ));
    }
    let data = map
        .entry("data".into())
        .or_insert_with(|| YamlValue::Map(BTreeMap::new()));
    let YamlValue::Map(data) = data else {
        return Err(ConfigError("data must be a mapping".into()));
    };
    data.entry("snapshot_id".into())
        .or_insert_with(|| YamlValue::Str("synthetic".into()));
    // The empty fingerprint is the shipped unresolved synthetic identity, not a digest.
    data.entry("fingerprint".into())
        .or_insert_with(|| YamlValue::Str(String::new()));
    if !data.contains_key("subscriptions") {
        return Err(ConfigError(
            "configuration requires data.subscriptions".into(),
        ));
    }
    Ok(map)
}

pub fn validate_mapping(mapping: &BTreeMap<String, YamlValue>) -> Result<Value, ConfigError> {
    crate::models::build_backtest(mapping)
}

pub fn validate_document(document: &Value) -> Result<Value, ConfigError> {
    let YamlValue::Map(mapping) = json_to_yaml(document)? else {
        return Err(ConfigError("configuration root must be a mapping".into()));
    };
    validate_mapping(&mapping)
}
