use std::collections::BTreeMap;

use serde_json::Value;

use super::*;
use crate::env;
use crate::yaml::parse_yaml;

pub(crate) const VALID_SETTINGS: &str = "\
schema_version: 1
provider:
  name: alpaca
  request:
    max_concurrency: 4
    timeout_seconds: 30
    retry:
      max_attempts: 5
      honor_retry_after: true
storage:
  parquet:
    compression: zstd
    file_granularity: symbol_session_data_type
  capacity_bytes: 1099511627776
  pause_enqueue_at_capacity: true
  pause_ingest_at_capacity: true
  lifecycle:
    enabled: true
    cool_after_days: 14
    cold:
      enabled: false
      minimum_age_days: 365
ingest:
  backfill:
    max_replicas: 1
    order: newest_first
    liquidity_rank_tiebreaker: true
    asset_priority:
      - equity_catch_up
      - equity_backfill
      - options_backfill
      - crypto_backfill
  queue:
    visibility_timeout_seconds: 300
    visibility_renewal_seconds: 60
    max_delivery_attempts: 5
    dead_letter_on_exhaustion: true
api:
  ensure_wait_deadline_seconds: 20
  sas:
    ttl_seconds: 900
    read_only: true
    object_scoped: true
  sql:
    max_scan_bytes: 1073741824
    max_result_bytes: 33554432
    authorized_force_override_default: false
  auth:
    entra_tenant_id: null
    entra_audience: null
    entra_role: Lake.Access
operations:
  monthly_budget_alerts_usd:
    - 40
    - 70
  log_retention_days: 30
  daily_coverage_report: true
";

fn document(text: &str) -> Value {
    parse_yaml(text).expect("yaml")
}

fn load_with(text: &str, overrides: &[(&str, &str)]) -> Result<LakeSettings, ConfigError> {
    let mut value = document(text);
    let environ: BTreeMap<String, String> = overrides
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect();
    env::apply(&mut value, &environ, ENV_SCHEMA)?;
    LakeSettings::from_value(value)
}

#[test]
fn loads_every_required_setting() {
    let settings = load_with(VALID_SETTINGS, &[]).expect("settings");
    assert_eq!(settings.schema_version, 1);
    assert_eq!(settings.provider.request.max_concurrency, 4);
    assert_eq!(settings.provider.request.requests_per_minute, None);
    assert!(settings.provider.api_key.is_none());
    assert_eq!(settings.storage.capacity_bytes, 1_099_511_627_776);
    assert!(!settings.storage.lifecycle.cold.enabled);
    assert_eq!(settings.ingest.backfill.asset_priority.len(), 4);
    assert_eq!(settings.api.auth.entra_role, "Lake.Access");
    assert_eq!(settings.api.auth.entra_tenant_id, None);
    assert_eq!(settings.operations.monthly_budget_alerts_usd, vec![40, 70]);
}

#[test]
fn environment_overrides_yaml_case_insensitively() {
    let settings = load_with(
        VALID_SETTINGS,
        &[
            ("lake_provider__request__max_concurrency", "8"),
            ("LAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE", "6000"),
            ("LAKE_API__BEARER_TOKEN", "token-value"),
            ("LAKE_PROVIDER__API_KEY", "key-value"),
        ],
    )
    .expect("settings");
    assert_eq!(settings.provider.request.max_concurrency, 8);
    assert_eq!(settings.provider.request.requests_per_minute, Some(6000));
    assert_eq!(settings.api.bearer_token.as_deref(), Some("token-value"));
    assert_eq!(settings.provider.api_key.as_deref(), Some("key-value"));
}

#[test]
fn unknown_settings_are_rejected() {
    let error = load_with(&format!("{VALID_SETTINGS}extra: 1\n"), &[]).expect_err("unknown");
    assert!(error.message.contains("unknown setting: extra"));
}

#[test]
fn missing_required_settings_are_rejected() {
    let text = VALID_SETTINGS.replace("    max_concurrency: 4\n", "");
    let error = load_with(&text, &[]).expect_err("missing");
    assert!(error
        .message
        .contains("missing required setting: provider.request.max_concurrency"));
}

#[test]
fn out_of_bounds_values_are_rejected() {
    let text = VALID_SETTINGS.replace("max_concurrency: 4", "max_concurrency: 1025");
    assert!(load_with(&text, &[]).is_err());
    let text = VALID_SETTINGS.replace("ttl_seconds: 900", "ttl_seconds: 100000");
    assert!(load_with(&text, &[]).is_err());
    let text = VALID_SETTINGS.replace("max_attempts: 5", "max_attempts: 0");
    assert!(load_with(&text, &[]).is_err());
    let text = VALID_SETTINGS.replace("max_result_bytes: 33554432", "max_result_bytes: 9999999999");
    assert!(load_with(&text, &[]).is_err());
}

#[test]
fn empty_runtime_credentials_are_rejected() {
    let error = load_with(VALID_SETTINGS, &[("LAKE_PROVIDER__API_KEY", "  ")])
        .expect_err("empty credential");
    assert!(error.message.contains("runtime secrets cannot be empty"));
}

#[test]
fn duplicate_asset_priority_is_rejected() {
    let text = VALID_SETTINGS.replace("      - crypto_backfill\n", "      - equity_backfill\n");
    let error = load_with(&text, &[]).expect_err("duplicate");
    assert!(error.message.contains("must not contain duplicates"));
}

#[test]
fn validate_accepts_a_loaded_value() {
    let settings = load_with(VALID_SETTINGS, &[]).expect("settings");
    settings.validate().expect("valid");
}
