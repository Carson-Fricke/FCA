//! Strict typed configuration models.
//!
//! Each builder validates one shell document and returns its canonical
//! `model_dump(mode="python")` tree: decimals become `{"$decimal": ...}`,
//! datetimes become `{"$datetime": ...}`, sequences become arrays, and mapped
//! values recurse. Validation mirrors `harness.configuration.models` closely
//! enough to reject the same malformed documents.

use std::collections::BTreeMap;
use std::str::FromStr;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde_json::{json, Map, Value};

use crate::yaml::YamlValue;
use crate::ConfigError;

type Mapping = BTreeMap<String, YamlValue>;

fn take(map: &mut Mapping, key: &str) -> Result<YamlValue, ConfigError> {
    map.remove(key)
        .ok_or_else(|| ConfigError(format!("Field required: {key}")))
}

fn take_opt(map: &mut Mapping, key: &str) -> Option<YamlValue> {
    map.remove(key)
}

fn ensure_empty(map: &Mapping, context: &str) -> Result<(), ConfigError> {
    if let Some((key, _)) = map.iter().next() {
        return Err(ConfigError(format!(
            "unexpected field {key:?} in {context}"
        )));
    }
    Ok(())
}

fn as_str(value: YamlValue, field: &str) -> Result<String, ConfigError> {
    match value {
        YamlValue::Str(text) => Ok(text),
        other => Err(ConfigError(format!(
            "{field} must be a string, got {}",
            kind(&other)
        ))),
    }
}

fn as_int(value: YamlValue, field: &str) -> Result<i64, ConfigError> {
    match value {
        YamlValue::Int(number) => Ok(number),
        other => Err(ConfigError(format!(
            "{field} must be an integer, got {}",
            kind(&other)
        ))),
    }
}

fn as_seq(value: YamlValue, field: &str) -> Result<Vec<YamlValue>, ConfigError> {
    match value {
        YamlValue::Seq(items) => Ok(items),
        other => Err(ConfigError(format!(
            "{field} must be a sequence, got {}",
            kind(&other)
        ))),
    }
}

fn as_map(value: YamlValue, field: &str) -> Result<Mapping, ConfigError> {
    match value {
        YamlValue::Map(mapping) => Ok(mapping),
        other => Err(ConfigError(format!(
            "{field} must be a mapping, got {}",
            kind(&other)
        ))),
    }
}

fn kind(value: &YamlValue) -> &'static str {
    match value {
        YamlValue::Null => "null",
        YamlValue::Bool(_) => "a boolean",
        YamlValue::Int(_) => "an integer",
        YamlValue::Decimal(_) => "a decimal",
        YamlValue::Str(_) => "a string",
        YamlValue::Seq(_) => "a sequence",
        YamlValue::Map(_) => "a mapping",
    }
}

fn scalar_text(value: YamlValue, field: &str) -> Result<String, ConfigError> {
    match value {
        YamlValue::Str(text) => Ok(text),
        YamlValue::Int(number) => Ok(number.to_string()),
        YamlValue::Decimal(text) => Ok(text),
        other => Err(ConfigError(format!(
            "{field} must be a scalar, got {}",
            kind(&other)
        ))),
    }
}

fn identifier(text: &str, field: &str) -> Result<String, ConfigError> {
    let trimmed = text.trim();
    if trimmed.is_empty() || trimmed.len() > 128 {
        return Err(ConfigError(format!("{field} must be 1 to 128 characters")));
    }
    let mut chars = trimmed.chars();
    let first = chars.next().unwrap_or(' ');
    if !first.is_ascii_alphanumeric() {
        return Err(ConfigError(format!("{field} has an invalid format")));
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')) {
        return Err(ConfigError(format!("{field} has an invalid format")));
    }
    Ok(trimmed.to_string())
}

fn non_empty_text(text: &str, field: &str) -> Result<String, ConfigError> {
    let trimmed = text.trim();
    if trimmed.is_empty() || trimmed.len() > 512 {
        return Err(ConfigError(format!("{field} must be 1 to 512 characters")));
    }
    Ok(trimmed.to_string())
}

fn sha256(text: &str, field: &str) -> Result<String, ConfigError> {
    if text.len() == 64
        && text
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Ok(text.to_string());
    }
    Err(ConfigError(format!(
        "{field} must be a 64-character hex digest"
    )))
}

fn env_reference(text: &str, field: &str) -> Result<String, ConfigError> {
    let inner = text
        .strip_prefix("${")
        .and_then(|value| value.strip_suffix('}'));
    let Some(inner) = inner else {
        return Err(ConfigError(format!("{field} must use ${{ENV_VAR}} syntax")));
    };
    let mut chars = inner.chars();
    let first = chars.next().unwrap_or(' ');
    if !(first.is_ascii_uppercase() || first == '_')
        || !chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
    {
        return Err(ConfigError(format!("{field} must use ${{ENV_VAR}} syntax")));
    }
    Ok(text.to_string())
}

fn currency(text: &str, field: &str) -> Result<String, ConfigError> {
    if text.len() == 3 && text.chars().all(|c| c.is_ascii_uppercase()) {
        return Ok(text.to_string());
    }
    Err(ConfigError(format!(
        "{field} must be a three-letter currency"
    )))
}

fn bar_interval(text: &str, field: &str) -> Result<String, ConfigError> {
    let bytes = text.as_bytes();
    if bytes.is_empty() || !(b'1'..=b'9').contains(&bytes[0]) {
        return Err(ConfigError(format!("{field} has an invalid interval")));
    }
    let mut index = 1usize;
    while index < bytes.len() && bytes[index].is_ascii_digit() {
        index += 1;
    }
    if matches!(&text[index..], "s" | "min" | "h" | "d") {
        return Ok(text.to_string());
    }
    Err(ConfigError(format!("{field} has an invalid interval")))
}

fn enum_value(text: &str, allowed: &[&str], field: &str) -> Result<String, ConfigError> {
    if allowed.contains(&text) {
        return Ok(text.to_string());
    }
    Err(ConfigError(format!(
        "{field} must be one of {allowed:?}, got {text:?}"
    )))
}

fn image_digest(text: &str, field: &str) -> Result<String, ConfigError> {
    if let Some(index) = text.rfind("@sha256:") {
        let prefix = &text[..index];
        let digest = &text[index + "@sha256:".len()..];
        if !prefix.is_empty()
            && !prefix.chars().any(char::is_whitespace)
            && digest.len() == 64
            && digest
                .chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return Ok(text.to_string());
        }
    }
    Err(ConfigError(format!(
        "{field} must use an immutable sha256 digest"
    )))
}

fn parse_decimal(text: &str, field: &str) -> Result<Decimal, ConfigError> {
    Decimal::from_str(text.trim())
        .map_err(|_| ConfigError(format!("{field} must be an exact decimal")))
}

fn decimal_value(decimal: Decimal) -> Value {
    json!({"$decimal": decimal.normalize().to_string()})
}

fn datetime_value(text: &str, field: &str) -> Result<Value, ConfigError> {
    let parsed = DateTime::parse_from_rfc3339(text).map_err(|_| {
        ConfigError(format!(
            "{field} must be an ISO 8601 datetime with a UTC offset"
        ))
    })?;
    let formatted = parsed
        .with_timezone(&Utc)
        .format("%Y-%m-%dT%H:%M:%S%.6fZ")
        .to_string();
    Ok(json!({"$datetime": formatted}))
}

fn config_value(value: &YamlValue) -> Result<Value, ConfigError> {
    match value {
        YamlValue::Null => Ok(Value::Null),
        YamlValue::Bool(flag) => Ok(Value::Bool(*flag)),
        YamlValue::Int(number) => Ok(json!(number)),
        YamlValue::Decimal(text) => {
            let decimal = parse_decimal(text, "value")?;
            Ok(decimal_value(decimal))
        }
        YamlValue::Str(text) => Ok(Value::String(text.clone())),
        YamlValue::Seq(items) => {
            let mut output = Vec::with_capacity(items.len());
            for item in items {
                output.push(config_value(item)?);
            }
            Ok(Value::Array(output))
        }
        YamlValue::Map(map) => {
            let mut output = Map::new();
            for (key, item) in map {
                output.insert(key.clone(), config_value(item)?);
            }
            Ok(Value::Object(output))
        }
    }
}

fn string_list(value: YamlValue, field: &str) -> Result<Value, ConfigError> {
    let items = as_seq(value, field)?;
    let mut output = Vec::with_capacity(items.len());
    for item in items {
        output.push(Value::String(as_str(item, field)?));
    }
    Ok(Value::Array(output))
}

fn symbols(value: YamlValue, field: &str) -> Result<Value, ConfigError> {
    let items = as_seq(value, field)?;
    let mut canonical = Vec::with_capacity(items.len());
    for item in items {
        let text = as_str(item, field)?;
        let canonical_symbol = text.trim().to_uppercase();
        if canonical_symbol.is_empty() {
            return Err(ConfigError(format!("{field} symbols must not be empty")));
        }
        canonical.push(canonical_symbol);
    }
    canonical.sort();
    canonical.dedup();
    if canonical.is_empty() {
        return Err(ConfigError(format!("{field} symbols must not be empty")));
    }
    Ok(Value::Array(
        canonical.into_iter().map(Value::String).collect(),
    ))
}

fn build_parameters(value: Option<YamlValue>, field: &str) -> Result<Value, ConfigError> {
    match value {
        None | Some(YamlValue::Null) => Ok(json!({})),
        Some(YamlValue::Map(map)) => config_value(&YamlValue::Map(map)),
        Some(other) => Err(ConfigError(format!(
            "{field} must be a mapping, got {}",
            kind(&other)
        ))),
    }
}

fn build_strategy_instance(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "strategy_instances[]")?;
    let instance_id = identifier(
        &as_str(take(&mut map, "instance_id")?, "instance_id")?,
        "instance_id",
    )?;
    let implementation = non_empty_text(
        &as_str(take(&mut map, "implementation")?, "implementation")?,
        "implementation",
    )?;
    let bundle_hash = sha256(
        &as_str(take(&mut map, "bundle_hash")?, "bundle_hash")?,
        "bundle_hash",
    )?;
    let capital_fraction_text =
        scalar_text(take(&mut map, "capital_fraction")?, "capital_fraction")?;
    let capital_fraction = parse_decimal(&capital_fraction_text, "capital_fraction")?;
    if capital_fraction <= Decimal::ZERO || capital_fraction > Decimal::ONE {
        return Err(ConfigError(
            "capital_fraction must be greater than zero and at most one".to_string(),
        ));
    }
    let symbols = symbols(take(&mut map, "symbols")?, "symbols")?;
    let parameters = build_parameters(take_opt(&mut map, "parameters"), "parameters")?;
    ensure_empty(&map, "strategy instance")?;
    Ok(json!({
        "instance_id": instance_id,
        "implementation": implementation,
        "bundle_hash": bundle_hash,
        "capital_fraction": decimal_value(capital_fraction),
        "symbols": symbols,
        "parameters": parameters,
    }))
}

fn build_strategy_instances(value: YamlValue) -> Result<(Value, Decimal), ConfigError> {
    let items = as_seq(value, "strategy_instances")?;
    if items.is_empty() {
        return Err(ConfigError(
            "strategy_instances must not be empty".to_string(),
        ));
    }
    let mut output = Vec::with_capacity(items.len());
    let mut ids: Vec<String> = Vec::new();
    let mut capital = Decimal::ZERO;
    for item in items {
        let built = build_strategy_instance(item)?;
        let id = built["instance_id"]
            .as_str()
            .unwrap_or_default()
            .to_string();
        ids.push(id);
        if let Some(fraction) = built["capital_fraction"]
            .get("$decimal")
            .and_then(Value::as_str)
        {
            capital += parse_decimal(fraction, "capital_fraction")?;
        }
        output.push(built);
    }
    let mut sorted = ids.clone();
    sorted.sort();
    sorted.dedup();
    if sorted.len() != ids.len() {
        return Err(ConfigError(
            "strategy instance_id values must be unique".to_string(),
        ));
    }
    if capital > Decimal::ONE {
        return Err(ConfigError(
            "strategy capital fractions must not exceed one".to_string(),
        ));
    }
    Ok((Value::Array(output), capital))
}

fn build_risk_rule(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "risk_pipeline.rules[]")?;
    let rule_id = identifier(&as_str(take(&mut map, "rule_id")?, "rule_id")?, "rule_id")?;
    let implementation = non_empty_text(
        &as_str(take(&mut map, "implementation")?, "implementation")?,
        "implementation",
    )?;
    let parameters = build_parameters(take_opt(&mut map, "parameters"), "parameters")?;
    ensure_empty(&map, "risk rule")?;
    Ok(json!({
        "rule_id": rule_id,
        "implementation": implementation,
        "parameters": parameters,
    }))
}

fn build_risk_pipeline(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "risk_pipeline")?;
    let on_reject = enum_value(
        &as_str(take(&mut map, "on_reject")?, "on_reject")?,
        &["reject_order", "halt_strategy", "halt_runtime"],
        "on_reject",
    )?;
    let rules_value = as_seq(take(&mut map, "rules")?, "rules")?;
    if rules_value.is_empty() {
        return Err(ConfigError(
            "risk_pipeline.rules must not be empty".to_string(),
        ));
    }
    let mut rules = Vec::with_capacity(rules_value.len());
    let mut ids: Vec<String> = Vec::new();
    for rule in rules_value {
        let built = build_risk_rule(rule)?;
        ids.push(built["rule_id"].as_str().unwrap_or_default().to_string());
        rules.push(built);
    }
    let mut sorted = ids.clone();
    sorted.sort();
    sorted.dedup();
    if sorted.len() != ids.len() {
        return Err(ConfigError(
            "risk rule_id values must be unique".to_string(),
        ));
    }
    ensure_empty(&map, "risk_pipeline")?;
    Ok(json!({"on_reject": on_reject, "rules": rules}))
}

fn build_fee_rule(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "fees.rules[]")?;
    let fee_id = identifier(&as_str(take(&mut map, "fee_id")?, "fee_id")?, "fee_id")?;
    let basis = enum_value(
        &as_str(take(&mut map, "basis")?, "basis")?,
        &["flat", "per_unit", "notional_bps"],
        "basis",
    )?;
    let rate = parse_decimal(&scalar_text(take(&mut map, "rate")?, "rate")?, "rate")?;
    if rate < Decimal::ZERO {
        return Err(ConfigError("rate must not be negative".to_string()));
    }
    let minimum = parse_decimal(
        &scalar_text(take(&mut map, "minimum")?, "minimum")?,
        "minimum",
    )?;
    if minimum < Decimal::ZERO {
        return Err(ConfigError("minimum must not be negative".to_string()));
    }
    let side = enum_value(
        &as_str(take(&mut map, "side")?, "side")?,
        &["buy", "sell", "both"],
        "side",
    )?;
    ensure_empty(&map, "fee rule")?;
    Ok(json!({
        "fee_id": fee_id,
        "basis": basis,
        "rate": decimal_value(rate),
        "minimum": decimal_value(minimum),
        "side": side,
    }))
}

fn build_fees(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "fees")?;
    let currency = currency(
        &as_str(take(&mut map, "currency")?, "currency")?,
        "currency",
    )?;
    let rounding = enum_value(
        &as_str(take(&mut map, "rounding")?, "rounding")?,
        &["half_even"],
        "rounding",
    )?;
    let rules_value = as_seq(take(&mut map, "rules")?, "rules")?;
    if rules_value.is_empty() {
        return Err(ConfigError("fees.rules must not be empty".to_string()));
    }
    let mut rules = Vec::with_capacity(rules_value.len());
    let mut ids: Vec<String> = Vec::new();
    for rule in rules_value {
        let built = build_fee_rule(rule)?;
        ids.push(built["fee_id"].as_str().unwrap_or_default().to_string());
        rules.push(built);
    }
    let mut sorted = ids.clone();
    sorted.sort();
    sorted.dedup();
    if sorted.len() != ids.len() {
        return Err(ConfigError("fee_id values must be unique".to_string()));
    }
    ensure_empty(&map, "fees")?;
    Ok(json!({"currency": currency, "rounding": rounding, "rules": rules}))
}

fn build_resources(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "deployment.resources")?;
    let cpu_text = scalar_text(take(&mut map, "cpu_cores")?, "cpu_cores")?;
    let cpu = parse_decimal(&cpu_text, "cpu_cores")?;
    if cpu <= Decimal::ZERO {
        return Err(ConfigError("cpu_cores must be positive".to_string()));
    }
    let memory_mib = as_int(take(&mut map, "memory_mib")?, "memory_mib")?;
    if memory_mib <= 0 {
        return Err(ConfigError("memory_mib must be positive".to_string()));
    }
    ensure_empty(&map, "deployment.resources")?;
    Ok(json!({"cpu_cores": decimal_value(cpu), "memory_mib": memory_mib}))
}

fn build_deployment(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "deployment")?;
    let name = identifier(&as_str(take(&mut map, "name")?, "name")?, "name")?;
    let target = enum_value(
        &as_str(take(&mut map, "target")?, "target")?,
        &["local", "azure_container_app", "azure_container_job"],
        "target",
    )?;
    let environment = enum_value(
        &as_str(take(&mut map, "environment")?, "environment")?,
        &["local", "development", "staging", "production"],
        "environment",
    )?;
    let image = match take(&mut map, "image")? {
        YamlValue::Null => Value::Null,
        YamlValue::Str(text) => Value::String(image_digest(&text, "image")?),
        other => {
            return Err(ConfigError(format!(
                "image must be a string or null, got {}",
                kind(&other)
            )))
        }
    };
    let replicas = as_int(take(&mut map, "replicas")?, "replicas")?;
    if !(0..=100).contains(&replicas) {
        return Err(ConfigError(
            "replicas must be between 0 and 100".to_string(),
        ));
    }
    let restart_policy = enum_value(
        &as_str(take(&mut map, "restart_policy")?, "restart_policy")?,
        &["never", "on_failure", "always"],
        "restart_policy",
    )?;
    let network_access = enum_value(
        &as_str(take(&mut map, "network_access")?, "network_access")?,
        &["disabled", "runtime_only"],
        "network_access",
    )?;
    let resources = build_resources(take(&mut map, "resources")?)?;
    ensure_empty(&map, "deployment")?;
    if target != "local" && image.is_null() {
        return Err(ConfigError(
            "non-local deployments require an immutable image".to_string(),
        ));
    }
    Ok(json!({
        "name": name,
        "target": target,
        "environment": environment,
        "image": image,
        "replicas": replicas,
        "restart_policy": restart_policy,
        "network_access": network_access,
        "resources": resources,
    }))
}

fn build_data_snapshot(value: YamlValue) -> Result<Value, ConfigError> {
    let mut map = as_map(value, "data")?;
    let snapshot_id = non_empty_text(
        &as_str(take(&mut map, "snapshot_id")?, "snapshot_id")?,
        "snapshot_id",
    )?;
    let fingerprint = sha256(
        &as_str(take(&mut map, "fingerprint")?, "fingerprint")?,
        "fingerprint",
    )?;
    let symbols = symbols(take(&mut map, "symbols")?, "symbols")?;
    let bar_interval = bar_interval(
        &as_str(take(&mut map, "bar_interval")?, "bar_interval")?,
        "bar_interval",
    )?;
    ensure_empty(&map, "data")?;
    Ok(json!({
        "snapshot_id": snapshot_id,
        "fingerprint": fingerprint,
        "symbols": symbols,
        "bar_interval": bar_interval,
    }))
}

fn build_runtime_adapter(value: YamlValue, field: &str) -> Result<Value, ConfigError> {
    let mut map = as_map(value, field)?;
    let adapter = non_empty_text(&as_str(take(&mut map, "adapter")?, "adapter")?, "adapter")?;
    let credentials = match take_opt(&mut map, "credentials") {
        None | Some(YamlValue::Null) => Map::new(),
        Some(YamlValue::Map(entries)) => {
            let mut output = Map::new();
            for (key, entry) in entries {
                let name = identifier(&key, "credential name")?;
                let text = as_str(entry, "credentials")?;
                output.insert(name, Value::String(env_reference(&text, "credentials")?));
            }
            output
        }
        Some(other) => {
            return Err(ConfigError(format!(
                "credentials must be a mapping, got {}",
                kind(&other)
            )))
        }
    };
    let settings = build_parameters(take_opt(&mut map, "settings"), "settings")?;
    ensure_empty(&map, field)?;
    Ok(json!({
        "adapter": adapter,
        "credentials": Value::Object(credentials),
        "settings": settings,
    }))
}

fn take_shell(map: &mut Mapping, expected: &str) -> Result<(), ConfigError> {
    let version = as_int(take(map, "schema_version")?, "schema_version")?;
    if version != 1 {
        return Err(ConfigError("schema_version must be 1".to_string()));
    }
    let shell = as_str(take(map, "shell")?, "shell")?;
    if shell != expected {
        return Err(ConfigError(format!(
            "configuration shell must be '{expected}', not '{shell}'"
        )));
    }
    Ok(())
}

/// Build the canonical API configuration tree.
///
/// # Errors
///
/// Returns [`ConfigError`] for any validation failure.
pub fn build_api(mapping: &Mapping) -> Result<Value, ConfigError> {
    let mut map = mapping.clone();
    take_shell(&mut map, "api")?;
    let server = {
        let mut server = as_map(take(&mut map, "server")?, "server")?;
        let host = non_empty_text(&as_str(take(&mut server, "host")?, "host")?, "host")?;
        let port = as_int(take(&mut server, "port")?, "port")?;
        if !(1..=65_535).contains(&port) {
            return Err(ConfigError("port must be between 1 and 65535".to_string()));
        }
        let workers = as_int(take(&mut server, "workers")?, "workers")?;
        if workers <= 0 {
            return Err(ConfigError("workers must be positive".to_string()));
        }
        let cors_origins = string_list(take(&mut server, "cors_origins")?, "cors_origins")?;
        ensure_empty(&server, "server")?;
        json!({"host": host, "port": port, "workers": workers, "cors_origins": cors_origins})
    };
    let auth = {
        let mut auth = as_map(take(&mut map, "auth")?, "auth")?;
        let bearer = env_reference(
            &as_str(take(&mut auth, "bearer_token")?, "bearer_token")?,
            "bearer_token",
        )?;
        ensure_empty(&auth, "auth")?;
        json!({"bearer_token": bearer})
    };
    let deployment = build_deployment(take(&mut map, "deployment")?)?;
    ensure_empty(&map, "api configuration")?;
    Ok(json!({
        "schema_version": 1,
        "shell": "api",
        "server": server,
        "auth": auth,
        "deployment": deployment,
    }))
}

/// Build the canonical backtest configuration tree.
///
/// # Errors
///
/// Returns [`ConfigError`] for any validation failure.
pub fn build_backtest(mapping: &Mapping) -> Result<Value, ConfigError> {
    let mut map = mapping.clone();
    take_shell(&mut map, "backtest")?;
    let run_id = identifier(&as_str(take(&mut map, "run_id")?, "run_id")?, "run_id")?;
    let start = datetime_value(&as_str(take(&mut map, "start")?, "start")?, "start")?;
    let end = datetime_value(&as_str(take(&mut map, "end")?, "end")?, "end")?;
    if start["$datetime"].as_str().unwrap_or_default()
        >= end["$datetime"].as_str().unwrap_or_default()
    {
        return Err(ConfigError("backtest start must be before end".to_string()));
    }
    let clock = enum_value(
        &as_str(take(&mut map, "clock")?, "clock")?,
        &["event_time"],
        "clock",
    )?;
    let event_ordering = enum_value(
        &as_str(take(&mut map, "event_ordering")?, "event_ordering")?,
        &["timestamp_then_sequence_then_class_then_feed"],
        "event_ordering",
    )?;
    let seed = as_int(take(&mut map, "seed")?, "seed")?;
    if seed < 0 {
        return Err(ConfigError("seed must not be negative".to_string()));
    }
    let base_currency = currency(
        &as_str(take(&mut map, "base_currency")?, "base_currency")?,
        "base_currency",
    )?;
    let initial_cash = parse_decimal(
        &scalar_text(take(&mut map, "initial_cash")?, "initial_cash")?,
        "initial_cash",
    )?;
    if initial_cash <= Decimal::ZERO {
        return Err(ConfigError("initial_cash must be positive".to_string()));
    }
    let data = build_data_snapshot(take(&mut map, "data")?)?;
    let (strategy_instances, _) = build_strategy_instances(take(&mut map, "strategy_instances")?)?;
    let risk_pipeline = build_risk_pipeline(take(&mut map, "risk_pipeline")?)?;
    let fees = build_fees(take(&mut map, "fees")?)?;
    let deployment = build_deployment(take(&mut map, "deployment")?)?;
    ensure_empty(&map, "backtest configuration")?;
    if deployment["network_access"].as_str() != Some("disabled") {
        return Err(ConfigError(
            "backtest deployments must disable network access".to_string(),
        ));
    }
    if deployment["replicas"].as_i64() != Some(1) {
        return Err(ConfigError(
            "backtest deployments must use exactly one replica".to_string(),
        ));
    }
    Ok(json!({
        "schema_version": 1,
        "shell": "backtest",
        "run_id": run_id,
        "start": start,
        "end": end,
        "clock": clock,
        "event_ordering": event_ordering,
        "seed": seed,
        "base_currency": base_currency,
        "initial_cash": decimal_value(initial_cash),
        "data": data,
        "strategy_instances": strategy_instances,
        "risk_pipeline": risk_pipeline,
        "fees": fees,
        "deployment": deployment,
    }))
}

/// Build the canonical trader configuration tree.
///
/// # Errors
///
/// Returns [`ConfigError`] for any validation failure.
pub fn build_trader(mapping: &Mapping) -> Result<Value, ConfigError> {
    let mut map = mapping.clone();
    take_shell(&mut map, "trader")?;
    let mode = enum_value(
        &as_str(take(&mut map, "mode")?, "mode")?,
        &["paper", "shadow", "live"],
        "mode",
    )?;
    let seed = as_int(take(&mut map, "seed")?, "seed")?;
    if seed < 0 {
        return Err(ConfigError("seed must not be negative".to_string()));
    }
    let base_currency = currency(
        &as_str(take(&mut map, "base_currency")?, "base_currency")?,
        "base_currency",
    )?;
    let (strategy_instances, _) = build_strategy_instances(take(&mut map, "strategy_instances")?)?;
    let risk_pipeline = build_risk_pipeline(take(&mut map, "risk_pipeline")?)?;
    let fees = build_fees(take(&mut map, "fees")?)?;
    let market_data = build_runtime_adapter(take(&mut map, "market_data")?, "market_data")?;
    let broker = build_runtime_adapter(take(&mut map, "broker")?, "broker")?;
    let deployment = build_deployment(take(&mut map, "deployment")?)?;
    ensure_empty(&map, "trader configuration")?;
    Ok(json!({
        "schema_version": 1,
        "shell": "trader",
        "mode": mode,
        "seed": seed,
        "base_currency": base_currency,
        "strategy_instances": strategy_instances,
        "risk_pipeline": risk_pipeline,
        "fees": fees,
        "market_data": market_data,
        "broker": broker,
        "deployment": deployment,
    }))
}

#[cfg(test)]
#[path = "../tests/unit/models.rs"]
mod tests;
