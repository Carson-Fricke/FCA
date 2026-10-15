//! Typed market-lake settings loaded from non-secret YAML and the environment.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{Map, Value};

use crate::env::{self, EnvKind};
use crate::error::ConfigError;
use crate::market_lake_config_dir;
use crate::yaml::load_yaml_mapping;

const ENV_SCHEMA: &[(&str, EnvKind)] = &[
    ("schema_version", EnvKind::Int),
    ("provider.name", EnvKind::Str),
    ("provider.request.max_concurrency", EnvKind::Uint),
    ("provider.request.timeout_seconds", EnvKind::Float),
    ("provider.request.requests_per_minute", EnvKind::Uint),
    ("provider.request.retry.max_attempts", EnvKind::Uint),
    ("provider.request.retry.honor_retry_after", EnvKind::Bool),
    ("provider.api_key", EnvKind::Str),
    ("provider.api_secret", EnvKind::Str),
    ("storage.parquet.compression", EnvKind::Str),
    ("storage.parquet.file_granularity", EnvKind::Str),
    ("storage.capacity_bytes", EnvKind::Uint),
    ("storage.pause_enqueue_at_capacity", EnvKind::Bool),
    ("storage.pause_ingest_at_capacity", EnvKind::Bool),
    ("storage.lifecycle.enabled", EnvKind::Bool),
    ("storage.lifecycle.cool_after_days", EnvKind::Uint),
    ("storage.lifecycle.cold.enabled", EnvKind::Bool),
    ("storage.lifecycle.cold.minimum_age_days", EnvKind::Uint),
    ("ingest.backfill.max_replicas", EnvKind::Uint),
    ("ingest.backfill.order", EnvKind::Str),
    ("ingest.backfill.liquidity_rank_tiebreaker", EnvKind::Bool),
    ("ingest.backfill.asset_priority", EnvKind::StrList),
    ("api.sas.ttl_seconds", EnvKind::Uint),
    ("api.sas.read_only", EnvKind::Bool),
    ("api.sas.object_scoped", EnvKind::Bool),
    ("api.sql.max_scan_bytes", EnvKind::Uint),
    ("api.sql.max_result_bytes", EnvKind::Uint),
    ("api.sql.authorized_force_override_default", EnvKind::Bool),
    ("api.auth.entra_tenant_id", EnvKind::Str),
    ("api.auth.entra_audience", EnvKind::Str),
    ("api.auth.entra_role", EnvKind::Str),
    ("api.bearer_token", EnvKind::Str),
    ("operations.monthly_budget_alerts_usd", EnvKind::IntList),
    ("operations.log_retention_days", EnvKind::Uint),
    ("operations.daily_coverage_report", EnvKind::Bool),
];

/// Complete typed representation of `settings.yaml`.
#[derive(Clone, Debug, PartialEq)]
pub struct LakeSettings {
    /// Configuration schema version; always 1.
    pub schema_version: i64,
    /// Provider client configuration.
    pub provider: ProviderSettings,
    /// Storage configuration.
    pub storage: StorageSettings,
    /// Ingest configuration.
    pub ingest: IngestSettings,
    /// HTTP API configuration.
    pub api: ApiSettings,
    /// Operations reporting configuration.
    pub operations: OperationsSettings,
}

/// Provider client configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct ProviderSettings {
    /// Provider name; always `alpaca`.
    pub name: String,
    /// HTTP request limits.
    pub request: RequestSettings,
    /// Retry policy.
    pub retry: RetrySettings,
    /// Runtime credential, environment-only.
    pub api_key: Option<String>,
    /// Runtime credential, environment-only.
    pub api_secret: Option<String>,
}

/// HTTP request limits.
#[derive(Clone, Debug, PartialEq)]
pub struct RequestSettings {
    /// Maximum concurrent in-flight requests (1..=1024).
    pub max_concurrency: u32,
    /// Per-request timeout in seconds (0..=3600).
    pub timeout_seconds: f64,
    /// Optional process-wide requests-per-minute budget.
    pub requests_per_minute: Option<u32>,
}

/// Retry policy.
#[derive(Clone, Debug, PartialEq)]
pub struct RetrySettings {
    /// Maximum attempts per request (1..=20).
    pub max_attempts: u32,
    /// Whether a 429 `Retry-After` response header is honored.
    pub honor_retry_after: bool,
}

/// Storage configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct StorageSettings {
    /// Parquet encoding settings.
    pub parquet: ParquetSettings,
    /// Capacity ceiling in bytes.
    pub capacity_bytes: u64,
    /// Whether enqueue pauses at capacity.
    pub pause_enqueue_at_capacity: bool,
    /// Whether ingest pauses at capacity.
    pub pause_ingest_at_capacity: bool,
    /// Lifecycle policy.
    pub lifecycle: LifecycleSettings,
}

/// Parquet encoding settings.
#[derive(Clone, Debug, PartialEq)]
pub struct ParquetSettings {
    /// Compression codec.
    pub compression: String,
    /// Object granularity; always `symbol_session_data_type`.
    pub file_granularity: String,
}

/// Lifecycle policy.
#[derive(Clone, Debug, PartialEq)]
pub struct LifecycleSettings {
    /// Whether lifecycle management is enabled.
    pub enabled: bool,
    /// Days before an untouched blob moves to Cool.
    pub cool_after_days: u64,
    /// Cold-tier policy.
    pub cold: ColdSettings,
}

/// Cold-tier policy.
#[derive(Clone, Debug, PartialEq)]
pub struct ColdSettings {
    /// Whether cold tier is enabled.
    pub enabled: bool,
    /// Minimum age before a cold transition.
    pub minimum_age_days: u64,
}

/// Ingest configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct IngestSettings {
    /// Backfill scheduling policy.
    pub backfill: BackfillSettings,
}

/// Backfill scheduling policy.
#[derive(Clone, Debug, PartialEq)]
pub struct BackfillSettings {
    /// Maximum concurrent replicas.
    pub max_replicas: u32,
    /// Ordering; always `newest_first`.
    pub order: String,
    /// Whether liquidity rank breaks ties.
    pub liquidity_rank_tiebreaker: bool,
    /// Asset-class priority, without duplicates.
    pub asset_priority: Vec<String>,
}

/// HTTP API configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct ApiSettings {
    /// SAS issuance settings.
    pub sas: SasSettings,
    /// SQL query limits.
    pub sql: SqlSettings,
    /// Entra authentication boundary.
    pub auth: AuthSettings,
    /// Runtime bearer token, environment-only.
    pub bearer_token: Option<String>,
}

/// SAS issuance settings.
#[derive(Clone, Debug, PartialEq)]
pub struct SasSettings {
    /// Token lifetime (1..=86400 seconds).
    pub ttl_seconds: u64,
    /// Always read-only.
    pub read_only: bool,
    /// Always object-scoped.
    pub object_scoped: bool,
}

/// SQL query limits.
#[derive(Clone, Debug, PartialEq)]
pub struct SqlSettings {
    /// Maximum estimated scan bytes.
    pub max_scan_bytes: u64,
    /// Maximum result bytes.
    pub max_result_bytes: u64,
    /// Whether force override is authorized by default.
    pub authorized_force_override_default: bool,
}

/// Entra authentication boundary.
#[derive(Clone, Debug, PartialEq)]
pub struct AuthSettings {
    /// Entra tenant id, when configured.
    pub entra_tenant_id: Option<String>,
    /// Entra audience, when configured.
    pub entra_audience: Option<String>,
    /// Required role claim.
    pub entra_role: String,
}

impl Default for AuthSettings {
    fn default() -> Self {
        Self {
            entra_tenant_id: None,
            entra_audience: None,
            entra_role: "Lake.Access".to_string(),
        }
    }
}

/// Operations reporting configuration.
#[derive(Clone, Debug, PartialEq)]
pub struct OperationsSettings {
    /// Monthly budget alert levels in USD.
    pub monthly_budget_alerts_usd: Vec<u64>,
    /// Log retention in days.
    pub log_retention_days: u32,
    /// Whether the daily report is enabled.
    pub daily_coverage_report: bool,
}

impl LakeSettings {
    /// Load `settings.yaml` with environment precedence.
    pub fn load(
        root: Option<&Path>,
        environ: &BTreeMap<String, String>,
    ) -> Result<Self, ConfigError> {
        let mut path = market_lake_config_dir(root, environ);
        path.push("settings.yaml");
        let mut document = load_yaml_mapping(&path)?;
        env::apply(&mut document, environ, ENV_SCHEMA)?;
        Self::from_value(document)
    }

    /// Load `settings.yaml` against the process environment.
    pub fn load_from_env(root: Option<&Path>) -> Result<Self, ConfigError> {
        let environ: BTreeMap<String, String> = std::env::vars().collect();
        Self::load(root, &environ)
    }

    /// Re-validate bounds on a constructed value.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.schema_version != 1 {
            return Err(ConfigError::new("schema_version must be 1"));
        }
        self.provider.validate()?;
        self.storage.validate()?;
        self.ingest.validate()?;
        self.api.validate()?;
        if self.operations.monthly_budget_alerts_usd.is_empty() {
            return Err(ConfigError::new(
                "operations.monthly_budget_alerts_usd must not be empty",
            ));
        }
        if self.operations.log_retention_days == 0 {
            return Err(ConfigError::new(
                "operations.log_retention_days must be positive",
            ));
        }
        Ok(())
    }

    fn from_value(value: Value) -> Result<Self, ConfigError> {
        let mut root = Section::new("", value)?;
        let schema_version = root.integer("schema_version")?;
        if schema_version != 1 {
            return Err(ConfigError::new("schema_version must be 1"));
        }
        let provider = ProviderSettings::from_section(root.object("provider")?)?;
        let storage = StorageSettings::from_section(root.object("storage")?)?;
        let ingest = IngestSettings::from_section(root.object("ingest")?)?;
        let api = ApiSettings::from_section(root.object("api")?)?;
        let operations = OperationsSettings::from_section(root.object("operations")?)?;
        root.finish()?;
        let settings = Self {
            schema_version,
            provider,
            storage,
            ingest,
            api,
            operations,
        };
        settings.validate()?;
        Ok(settings)
    }
}

impl ProviderSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let name = section.string("name")?;
        if name != "alpaca" {
            return Err(ConfigError::new("provider.name must be alpaca"));
        }
        let mut request_section = section.object("request")?;
        let retry = RetrySettings::from_section(request_section.object("retry")?)?;
        let request = RequestSettings::from_section(request_section)?;
        let api_key = optional_non_empty(section.opt_string("api_key")?)?;
        let api_secret = optional_non_empty(section.opt_string("api_secret")?)?;
        section.finish()?;
        Ok(Self {
            name,
            request,
            retry,
            api_key,
            api_secret,
        })
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if self.name != "alpaca" {
            return Err(ConfigError::new("provider.name must be alpaca"));
        }
        self.request.validate("provider.request")?;
        if !(1..=20).contains(&self.retry.max_attempts) {
            return Err(ConfigError::new(
                "provider.request.retry.max_attempts must be between 1 and 20",
            ));
        }
        Ok(())
    }
}

impl RequestSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let max_concurrency = section.uint("max_concurrency")? as u32;
        let timeout_seconds = section.float("timeout_seconds")?;
        let requests_per_minute = match section.take("requests_per_minute") {
            None | Some(Value::Null) => None,
            Some(value) => {
                Some(parse_uint_value(&value, "provider.request.requests_per_minute")? as u32)
            }
        };
        section.finish()?;
        let settings = Self {
            max_concurrency,
            timeout_seconds,
            requests_per_minute,
        };
        settings.validate("provider.request")?;
        Ok(settings)
    }

    fn validate(&self, path: &str) -> Result<(), ConfigError> {
        // The sliced fetch client's measured optimum is ~90-180 in-flight
        // requests (~220k rows/s against the provider); Python's 1..=64 bound
        // was calibrated for the sequential-cursor client and is raised here.
        if !(1..=1024).contains(&self.max_concurrency) {
            return Err(ConfigError::new(format!(
                "{path}.max_concurrency must be between 1 and 1024"
            )));
        }
        if !(self.timeout_seconds > 0.0 && self.timeout_seconds <= 3600.0) {
            return Err(ConfigError::new(format!(
                "{path}.timeout_seconds must be greater than 0 and at most 3600"
            )));
        }
        if self.requests_per_minute == Some(0) {
            return Err(ConfigError::new(format!(
                "{path}.requests_per_minute must be positive"
            )));
        }
        Ok(())
    }
}

impl RetrySettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let max_attempts = section.uint("max_attempts")? as u32;
        let honor_retry_after = section.boolean("honor_retry_after")?;
        section.finish()?;
        if !(1..=20).contains(&max_attempts) {
            return Err(ConfigError::new(
                "provider.request.retry.max_attempts must be between 1 and 20",
            ));
        }
        Ok(Self {
            max_attempts,
            honor_retry_after,
        })
    }
}

impl StorageSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let parquet = ParquetSettings::from_section(section.object("parquet")?)?;
        let capacity_bytes = section.uint("capacity_bytes")?;
        let pause_enqueue_at_capacity = section.boolean("pause_enqueue_at_capacity")?;
        let pause_ingest_at_capacity = section.boolean("pause_ingest_at_capacity")?;
        let lifecycle = LifecycleSettings::from_section(section.object("lifecycle")?)?;
        section.finish()?;
        let settings = Self {
            parquet,
            capacity_bytes,
            pause_enqueue_at_capacity,
            pause_ingest_at_capacity,
            lifecycle,
        };
        settings.validate()?;
        Ok(settings)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if self.parquet.file_granularity != "symbol_session_data_type" {
            return Err(ConfigError::new(
                "storage.parquet.file_granularity must be symbol_session_data_type",
            ));
        }
        const COMPRESSIONS: &[&str] = &["zstd", "snappy", "gzip", "brotli", "lz4", "uncompressed"];
        if !COMPRESSIONS.contains(&self.parquet.compression.as_str()) {
            return Err(ConfigError::new(format!(
                "storage.parquet.compression must be one of {COMPRESSIONS:?}"
            )));
        }
        if self.capacity_bytes == 0 {
            return Err(ConfigError::new("storage.capacity_bytes must be positive"));
        }
        if self.lifecycle.cold.minimum_age_days == 0 {
            return Err(ConfigError::new(
                "storage.lifecycle.cold.minimum_age_days must be positive",
            ));
        }
        Ok(())
    }
}

impl ParquetSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let compression = section.string("compression")?;
        let file_granularity = section.string("file_granularity")?;
        section.finish()?;
        Ok(Self {
            compression,
            file_granularity,
        })
    }
}

impl LifecycleSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let enabled = section.boolean("enabled")?;
        let cool_after_days = section.uint("cool_after_days")?;
        let cold = ColdSettings::from_section(section.object("cold")?)?;
        section.finish()?;
        Ok(Self {
            enabled,
            cool_after_days,
            cold,
        })
    }
}

impl ColdSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let enabled = section.boolean("enabled")?;
        let minimum_age_days = section.uint("minimum_age_days")?;
        section.finish()?;
        Ok(Self {
            enabled,
            minimum_age_days,
        })
    }
}

impl IngestSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let backfill = BackfillSettings::from_section(section.object("backfill")?)?;
        section.finish()?;
        Ok(Self { backfill })
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if self.backfill.max_replicas == 0 {
            return Err(ConfigError::new(
                "ingest.backfill.max_replicas must be positive",
            ));
        }
        if self.backfill.order != "newest_first" {
            return Err(ConfigError::new(
                "ingest.backfill.order must be newest_first",
            ));
        }
        const PRIORITIES: &[&str] = &[
            "equity_catch_up",
            "equity_backfill",
            "options_backfill",
            "crypto_backfill",
        ];
        if self.backfill.asset_priority.is_empty() {
            return Err(ConfigError::new(
                "ingest.backfill.asset_priority must not be empty",
            ));
        }
        for priority in &self.backfill.asset_priority {
            if !PRIORITIES.contains(&priority.as_str()) {
                return Err(ConfigError::new(format!(
                    "ingest.backfill.asset_priority contains an unsupported value: {priority:?}"
                )));
            }
        }
        let unique: std::collections::BTreeSet<&String> =
            self.backfill.asset_priority.iter().collect();
        if unique.len() != self.backfill.asset_priority.len() {
            return Err(ConfigError::new(
                "ingest.backfill.asset_priority must not contain duplicates",
            ));
        }
        Ok(())
    }
}

impl BackfillSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let max_replicas = section.uint("max_replicas")? as u32;
        let order = section.string("order")?;
        let liquidity_rank_tiebreaker = section.boolean("liquidity_rank_tiebreaker")?;
        let asset_priority = section.string_list("asset_priority")?;
        section.finish()?;
        Ok(Self {
            max_replicas,
            order,
            liquidity_rank_tiebreaker,
            asset_priority,
        })
    }
}

impl ApiSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let sas = SasSettings::from_section(section.object("sas")?)?;
        let sql = SqlSettings::from_section(section.object("sql")?)?;
        let auth = match section.take("auth") {
            None | Some(Value::Null) => AuthSettings::default(),
            Some(value) => AuthSettings::from_value(value)?,
        };
        let bearer_token = optional_non_empty(section.opt_string("bearer_token")?)?;
        section.finish()?;
        let settings = Self {
            sas,
            sql,
            auth,
            bearer_token,
        };
        settings.validate()?;
        Ok(settings)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if !(1..=86_400).contains(&self.sas.ttl_seconds) {
            return Err(ConfigError::new(
                "api.sas.ttl_seconds must be between 1 and 86400",
            ));
        }
        if !self.sas.read_only || !self.sas.object_scoped {
            return Err(ConfigError::new(
                "api.sas.read_only and api.sas.object_scoped must both be true",
            ));
        }
        if self.sql.max_scan_bytes == 0 || self.sql.max_result_bytes == 0 {
            return Err(ConfigError::new("api.sql limits must be positive"));
        }
        if self.sql.max_result_bytes > self.sql.max_scan_bytes {
            return Err(ConfigError::new(
                "api.sql.max_result_bytes cannot exceed max_scan_bytes",
            ));
        }
        if self.auth.entra_role.trim().is_empty() {
            return Err(ConfigError::new("entra_role must be a non-empty string"));
        }
        Ok(())
    }
}

impl SasSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let ttl_seconds = section.uint("ttl_seconds")?;
        let read_only = section.boolean("read_only")?;
        let object_scoped = section.boolean("object_scoped")?;
        section.finish()?;
        Ok(Self {
            ttl_seconds,
            read_only,
            object_scoped,
        })
    }
}

impl SqlSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let max_scan_bytes = section.uint("max_scan_bytes")?;
        let max_result_bytes = section.uint("max_result_bytes")?;
        let authorized_force_override_default =
            section.boolean("authorized_force_override_default")?;
        section.finish()?;
        Ok(Self {
            max_scan_bytes,
            max_result_bytes,
            authorized_force_override_default,
        })
    }
}

impl AuthSettings {
    fn from_value(value: Value) -> Result<Self, ConfigError> {
        let mut section = Section::new("api.auth", value)?;
        let entra_tenant_id =
            blank_to_none(section.opt_string("entra_tenant_id")?, "entra_tenant_id")?;
        let entra_audience =
            blank_to_none(section.opt_string("entra_audience")?, "entra_audience")?;
        let entra_role = match section.opt_string("entra_role")? {
            Some(role) if !role.trim().is_empty() => role,
            Some(_) => {
                return Err(ConfigError::new("entra_role must be a non-empty string"));
            }
            None => "Lake.Access".to_string(),
        };
        section.finish()?;
        Ok(Self {
            entra_tenant_id,
            entra_audience,
            entra_role,
        })
    }
}

impl OperationsSettings {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let monthly_budget_alerts_usd = section.int_list("monthly_budget_alerts_usd")?;
        if monthly_budget_alerts_usd.is_empty() {
            return Err(ConfigError::new(
                "operations.monthly_budget_alerts_usd must not be empty",
            ));
        }
        let mut alerts = Vec::with_capacity(monthly_budget_alerts_usd.len());
        for value in monthly_budget_alerts_usd {
            if value <= 0 {
                return Err(ConfigError::new(
                    "operations.monthly_budget_alerts_usd values must be positive",
                ));
            }
            alerts.push(value as u64);
        }
        let log_retention_days = section.uint("log_retention_days")? as u32;
        if log_retention_days == 0 {
            return Err(ConfigError::new(
                "operations.log_retention_days must be positive",
            ));
        }
        let daily_coverage_report = section.boolean("daily_coverage_report")?;
        section.finish()?;
        Ok(Self {
            monthly_budget_alerts_usd: alerts,
            log_retention_days,
            daily_coverage_report,
        })
    }
}

fn optional_non_empty(value: Option<String>) -> Result<Option<String>, ConfigError> {
    match value {
        None => Ok(None),
        Some(text) if text.trim().is_empty() => {
            Err(ConfigError::new("runtime secrets cannot be empty"))
        }
        Some(text) => Ok(Some(text)),
    }
}

fn blank_to_none(value: Option<String>, _path: &str) -> Result<Option<String>, ConfigError> {
    Ok(match value {
        Some(text) if text.trim().is_empty() => None,
        other => other,
    })
}

fn parse_uint_value(value: &Value, path: &str) -> Result<u64, ConfigError> {
    if let Some(number) = value.as_u64() {
        return Ok(number);
    }
    if let Some(number) = value.as_i64() {
        if number >= 0 {
            return Ok(number as u64);
        }
    }
    if let Some(number) = value.as_f64() {
        if number >= 0.0 && number.fract() == 0.0 {
            return Ok(number as u64);
        }
    }
    Err(ConfigError::new(format!(
        "{path} must be a non-negative integer"
    )))
}

/// A strict mapping reader that tracks consumed keys.
struct Section {
    path: String,
    map: Map<String, Value>,
}

impl Section {
    fn new(path: &str, value: Value) -> Result<Self, ConfigError> {
        match value {
            Value::Object(map) => Ok(Self {
                path: path.to_string(),
                map,
            }),
            _ => Err(ConfigError::new(format!(
                "{} must be a mapping",
                if path.is_empty() { "config root" } else { path }
            ))),
        }
    }

    fn take(&mut self, key: &str) -> Option<Value> {
        self.map.remove(key)
    }

    fn require(&mut self, key: &str) -> Result<Value, ConfigError> {
        self.take(key).ok_or_else(|| {
            ConfigError::new(format!("missing required setting: {}", self.child(key)))
        })
    }

    fn child(&self, key: &str) -> String {
        if self.path.is_empty() {
            key.to_string()
        } else {
            format!("{}.{}", self.path, key)
        }
    }

    fn object(&mut self, key: &str) -> Result<Section, ConfigError> {
        let value = self.require(key)?;
        Section::new(&self.child(key), value)
    }

    fn string(&mut self, key: &str) -> Result<String, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::String(text) => Ok(text),
            _ => Err(ConfigError::new(format!("{path} must be a string"))),
        }
    }

    fn opt_string(&mut self, key: &str) -> Result<Option<String>, ConfigError> {
        let path = self.child(key);
        match self.take(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(text)) => Ok(Some(text)),
            Some(_) => Err(ConfigError::new(format!("{path} must be a string"))),
        }
    }

    fn boolean(&mut self, key: &str) -> Result<bool, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::Bool(value) => Ok(value),
            _ => Err(ConfigError::new(format!("{path} must be a boolean"))),
        }
    }

    fn integer(&mut self, key: &str) -> Result<i64, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::Number(number) => {
                if let Some(value) = number.as_i64() {
                    return Ok(value);
                }
                if let Some(value) = number.as_f64() {
                    if value.fract() == 0.0 {
                        return Ok(value as i64);
                    }
                }
                Err(ConfigError::new(format!("{path} must be an integer")))
            }
            _ => Err(ConfigError::new(format!("{path} must be an integer"))),
        }
    }

    fn uint(&mut self, key: &str) -> Result<u64, ConfigError> {
        let path = self.child(key);
        let value = self.require(key)?;
        parse_uint_value(&value, &path)
    }

    fn float(&mut self, key: &str) -> Result<f64, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::Number(number) => number
                .as_f64()
                .ok_or_else(|| ConfigError::new(format!("{path} must be a number"))),
            _ => Err(ConfigError::new(format!("{path} must be a number"))),
        }
    }

    fn string_list(&mut self, key: &str) -> Result<Vec<String>, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::Array(items) => {
                let mut values = Vec::with_capacity(items.len());
                for item in items {
                    match item {
                        Value::String(text) => values.push(text),
                        _ => {
                            return Err(ConfigError::new(format!(
                                "{path} entries must be strings"
                            )));
                        }
                    }
                }
                Ok(values)
            }
            _ => Err(ConfigError::new(format!("{path} must be a list"))),
        }
    }

    fn int_list(&mut self, key: &str) -> Result<Vec<i64>, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::Array(items) => {
                let mut values = Vec::with_capacity(items.len());
                for item in items {
                    match item.as_i64() {
                        Some(number) => values.push(number),
                        None => {
                            return Err(ConfigError::new(format!(
                                "{path} entries must be integers"
                            )));
                        }
                    }
                }
                Ok(values)
            }
            _ => Err(ConfigError::new(format!("{path} must be a list"))),
        }
    }

    fn finish(self) -> Result<(), ConfigError> {
        if let Some(key) = self.map.keys().next() {
            let child = if self.path.is_empty() {
                key.clone()
            } else {
                format!("{}.{}", self.path, key)
            };
            return Err(ConfigError::new(format!("unknown setting: {child}")));
        }
        Ok(())
    }
}
