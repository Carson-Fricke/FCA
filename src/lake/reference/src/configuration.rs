//! Typed settings for point-in-time reference dataset ingestion.
//!
//! Ports `lake.reference.configuration`. Settings follow the market-lake
//! pattern: strict non-secret YAML defaults in `config/lake/reference.yaml`
//! with `LAKE_REFERENCE__` environment overrides taking precedence. The SEC
//! fair-access policy caps requests per second; the User-Agent must follow
//! the SEC `Sample Company Name AdminContact@domain.com` format and is not a
//! secret.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use lake_configuration::{load_yaml_mapping, ConfigError};
use serde_json::Value;

use crate::edgar::format_template;
use crate::error::ReferenceError;

/// Retry policy for reference provider requests.
#[derive(Clone, Debug, PartialEq)]
pub struct ReferenceRetrySettings {
    /// Maximum attempts per request (1..=20).
    pub max_attempts: u32,
    /// Whether a 429 `Retry-After` response header is honored.
    pub honor_retry_after: bool,
    /// Exponential backoff base in seconds (>= 0).
    pub backoff_seconds: f64,
    /// Maximum backoff in seconds (> 0).
    pub max_backoff_seconds: f64,
}

/// HTTP request policy for reference providers.
#[derive(Clone, Debug, PartialEq)]
pub struct ReferenceHttpSettings {
    /// SEC fair-access cap: no more than 10 requests per second.
    pub requests_per_second: f64,
    /// Whole-request timeout in seconds (1..=3600).
    pub timeout_seconds: u32,
    /// Retry policy.
    pub retry: ReferenceRetrySettings,
}

/// Complete typed representation of `config/lake/reference.yaml`.
#[derive(Clone, Debug, PartialEq)]
pub struct ReferenceSettings {
    /// Configuration schema version; always 1.
    pub schema_version: i64,
    /// Descriptive SEC User-Agent (`Name AdminContact@domain.com`).
    pub user_agent: String,
    /// The Nasdaq Trader symbol directory URL.
    pub classification_url: String,
    /// The EDGAR companyfacts URL template.
    pub company_facts_url_template: String,
    /// The EDGAR companyconcept URL template.
    pub company_concept_url_template: String,
    /// The nightly bulk companyfacts archive URL.
    pub bulk_company_facts_url: String,
    /// HTTP request policy.
    pub http: ReferenceHttpSettings,
}

impl ReferenceSettings {
    /// Build and validate settings from a parsed YAML/JSON mapping.
    pub fn from_value(value: &Value) -> Result<Self, ConfigError> {
        let object = value
            .as_object()
            .ok_or_else(|| ConfigError::new("reference settings root must be a mapping"))?;
        const KEYS: [&str; 7] = [
            "schema_version",
            "user_agent",
            "classification_url",
            "company_facts_url_template",
            "company_concept_url_template",
            "bulk_company_facts_url",
            "http",
        ];
        for key in object.keys() {
            if !KEYS.contains(&key.as_str()) {
                return Err(ConfigError::new(format!(
                    "reference settings has an unexpected field {key:?}"
                )));
            }
        }
        let schema_version = integer_of(object, "schema_version")?;
        let user_agent = string_of(object, "user_agent")?;
        let classification_url = string_of(object, "classification_url")?;
        let company_facts_url_template = string_of(object, "company_facts_url_template")?;
        let company_concept_url_template = string_of(object, "company_concept_url_template")?;
        let bulk_company_facts_url = string_of(object, "bulk_company_facts_url")?;
        let http_value = object
            .get("http")
            .ok_or_else(|| ConfigError::new("reference settings is missing the 'http' object"))?;
        let http = http_settings_from_value(http_value)?;
        let settings = Self {
            schema_version: i64::from(schema_version),
            user_agent,
            classification_url,
            company_facts_url_template,
            company_concept_url_template,
            bulk_company_facts_url,
            http,
        };
        settings.validate()?;
        Ok(settings)
    }

    /// Validate the settings against the SEC fair-access contract.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.schema_version != 1 {
            return Err(ConfigError::new("schema_version must be 1"));
        }
        let user_agent = self.user_agent.trim();
        if user_agent.is_empty() || !user_agent.contains('@') {
            return Err(ConfigError::new(
                "reference.user_agent must follow the SEC format \
                 'Sample Company Name AdminContact@domain.com'",
            ));
        }
        if !self.company_facts_url_template.contains("{cik") {
            return Err(ConfigError::new(
                "company_facts_url_template must contain the {cik} placeholder",
            ));
        }
        validate_template_value(&self.company_facts_url_template, &["cik"])?;
        for placeholder in ["cik", "taxonomy", "tag"] {
            if !self
                .company_concept_url_template
                .contains(&format!("{{{placeholder}"))
            {
                return Err(ConfigError::new(format!(
                    "company_concept_url_template must contain the {{{placeholder}}} placeholder"
                )));
            }
        }
        validate_template_value(
            &self.company_concept_url_template,
            &["cik", "taxonomy", "tag"],
        )?;
        if self.http.requests_per_second <= 0.0 || self.http.requests_per_second > 10.0 {
            return Err(ConfigError::new(
                "reference.http.requests_per_second must be greater than 0 and at most 10",
            ));
        }
        if !(1..=3600).contains(&self.http.timeout_seconds) {
            return Err(ConfigError::new(
                "reference.http.timeout_seconds must be between 1 and 3600",
            ));
        }
        if !(1..=20).contains(&self.http.retry.max_attempts) {
            return Err(ConfigError::new(
                "reference.http.retry.max_attempts must be between 1 and 20",
            ));
        }
        if self.http.retry.backoff_seconds < 0.0 {
            return Err(ConfigError::new(
                "reference.http.retry.backoff_seconds cannot be negative",
            ));
        }
        if self.http.retry.max_backoff_seconds <= 0.0 {
            return Err(ConfigError::new(
                "reference.http.retry.max_backoff_seconds must be positive",
            ));
        }
        Ok(())
    }

    /// Apply `LAKE_REFERENCE__` environment overrides over these defaults.
    ///
    /// Mirrors pydantic-settings: environment values win over the YAML
    /// defaults and are parsed into the field types.
    pub fn with_environment(
        mut self,
        environ: &BTreeMap<String, String>,
    ) -> Result<Self, ConfigError> {
        if let Some(value) = env_str(environ, "SCHEMA_VERSION") {
            self.schema_version = value.parse().map_err(|_| {
                ConfigError::new("LAKE_REFERENCE__SCHEMA_VERSION must be an integer")
            })?;
        }
        if let Some(value) = env_str(environ, "USER_AGENT") {
            self.user_agent = value;
        }
        if let Some(value) = env_str(environ, "CLASSIFICATION_URL") {
            self.classification_url = value;
        }
        if let Some(value) = env_str(environ, "COMPANY_FACTS_URL_TEMPLATE") {
            self.company_facts_url_template = value;
        }
        if let Some(value) = env_str(environ, "COMPANY_CONCEPT_URL_TEMPLATE") {
            self.company_concept_url_template = value;
        }
        if let Some(value) = env_str(environ, "BULK_COMPANY_FACTS_URL") {
            self.bulk_company_facts_url = value;
        }
        if let Some(value) = env_float(environ, "HTTP__REQUESTS_PER_SECOND")? {
            self.http.requests_per_second = value;
        }
        if let Some(value) = env_float(environ, "HTTP__TIMEOUT_SECONDS")? {
            let seconds = u32::try_from(value as i64).map_err(|_| {
                ConfigError::new("LAKE_REFERENCE__HTTP__TIMEOUT_SECONDS must be a positive integer")
            })?;
            self.http.timeout_seconds = seconds;
        }
        if let Some(value) = env_str(environ, "HTTP__RETRY__MAX_ATTEMPTS") {
            self.http.retry.max_attempts = value.parse().map_err(|_| {
                ConfigError::new("LAKE_REFERENCE__HTTP__RETRY__MAX_ATTEMPTS must be an integer")
            })?;
        }
        if let Some(value) = env_str(environ, "HTTP__RETRY__HONOR_RETRY_AFTER") {
            self.http.retry.honor_retry_after = parse_bool(&value).ok_or_else(|| {
                ConfigError::new("LAKE_REFERENCE__HTTP__RETRY__HONOR_RETRY_AFTER must be a boolean")
            })?;
        }
        if let Some(value) = env_float(environ, "HTTP__RETRY__BACKOFF_SECONDS")? {
            self.http.retry.backoff_seconds = value;
        }
        if let Some(value) = env_float(environ, "HTTP__RETRY__MAX_BACKOFF_SECONDS")? {
            self.http.retry.max_backoff_seconds = value;
        }
        self.validate()?;
        Ok(self)
    }
}

fn validate_template_value(template: &str, placeholders: &[&str]) -> Result<(), ConfigError> {
    let values: Vec<(&str, String)> = placeholders
        .iter()
        .map(|name| (*name, "x".to_string()))
        .collect();
    format_template(template, &values, "invalid URL template")
        .map(|_| ())
        .map_err(|error| ConfigError::new(error.to_string()))
}

fn http_settings_from_value(value: &Value) -> Result<ReferenceHttpSettings, ConfigError> {
    let object = value
        .as_object()
        .ok_or_else(|| ConfigError::new("reference http settings must be a mapping"))?;
    const KEYS: [&str; 3] = ["requests_per_second", "timeout_seconds", "retry"];
    for key in object.keys() {
        if !KEYS.contains(&key.as_str()) {
            return Err(ConfigError::new(format!(
                "reference http settings has an unexpected field {key:?}"
            )));
        }
    }
    let requests_per_second = float_of(object, "requests_per_second")?;
    let timeout_seconds = integer_of(object, "timeout_seconds")?;
    let retry_value = object
        .get("retry")
        .ok_or_else(|| ConfigError::new("reference http settings is missing the 'retry' object"))?;
    let retry = retry_settings_from_value(retry_value)?;
    Ok(ReferenceHttpSettings {
        requests_per_second,
        timeout_seconds,
        retry,
    })
}

fn retry_settings_from_value(value: &Value) -> Result<ReferenceRetrySettings, ConfigError> {
    let object = value
        .as_object()
        .ok_or_else(|| ConfigError::new("reference retry settings must be a mapping"))?;
    const KEYS: [&str; 4] = [
        "max_attempts",
        "honor_retry_after",
        "backoff_seconds",
        "max_backoff_seconds",
    ];
    for key in object.keys() {
        if !KEYS.contains(&key.as_str()) {
            return Err(ConfigError::new(format!(
                "reference retry settings has an unexpected field {key:?}"
            )));
        }
    }
    Ok(ReferenceRetrySettings {
        max_attempts: integer_of(object, "max_attempts")?,
        honor_retry_after: bool_of(object, "honor_retry_after")?,
        backoff_seconds: float_of(object, "backoff_seconds")?,
        max_backoff_seconds: float_of(object, "max_backoff_seconds")?,
    })
}

fn integer_of(object: &serde_json::Map<String, Value>, key: &str) -> Result<u32, ConfigError> {
    let value = object
        .get(key)
        .ok_or_else(|| ConfigError::new(format!("reference settings is missing {key:?}")))?;
    value
        .as_u64()
        .and_then(|integer| u32::try_from(integer).ok())
        .ok_or_else(|| ConfigError::new(format!("reference {key} must be an integer")))
}

fn float_of(object: &serde_json::Map<String, Value>, key: &str) -> Result<f64, ConfigError> {
    let value = object
        .get(key)
        .ok_or_else(|| ConfigError::new(format!("reference settings is missing {key:?}")))?;
    value
        .as_f64()
        .ok_or_else(|| ConfigError::new(format!("reference {key} must be a number")))
}

fn bool_of(object: &serde_json::Map<String, Value>, key: &str) -> Result<bool, ConfigError> {
    let value = object
        .get(key)
        .ok_or_else(|| ConfigError::new(format!("reference settings is missing {key:?}")))?;
    value
        .as_bool()
        .ok_or_else(|| ConfigError::new(format!("reference {key} must be a boolean")))
}

fn string_of(object: &serde_json::Map<String, Value>, key: &str) -> Result<String, ConfigError> {
    let value = object
        .get(key)
        .ok_or_else(|| ConfigError::new(format!("reference settings is missing {key:?}")))?;
    value
        .as_str()
        .map(str::to_string)
        .ok_or_else(|| ConfigError::new(format!("reference {key} must be a string")))
}

fn env_str(environ: &BTreeMap<String, String>, key: &str) -> Option<String> {
    environ
        .get(&format!("LAKE_REFERENCE__{key}"))
        .filter(|value| !value.is_empty())
        .cloned()
}

fn env_float(environ: &BTreeMap<String, String>, key: &str) -> Result<Option<f64>, ConfigError> {
    let Some(value) = env_str(environ, key) else {
        return Ok(None);
    };
    value
        .parse::<f64>()
        .map(Some)
        .map_err(|_| ConfigError::new(format!("LAKE_REFERENCE__{key} must be a number")))
}

fn parse_bool(value: &str) -> Option<bool> {
    match value.to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" => Some(true),
        "false" | "0" | "no" => Some(false),
        _ => None,
    }
}

/// Resolve the reference config directory without machine-specific paths.
///
/// Mirrors `reference_config_dir`: an explicit root may point directly at
/// `config/lake`, otherwise `config/lake` is appended to the repository root.
pub fn reference_config_dir(root: Option<&Path>) -> PathBuf {
    let root_path = match root {
        Some(path) => path.to_path_buf(),
        None => repository_root(),
    };
    if root_path.file_name().is_some_and(|name| name == "lake")
        && root_path
            .parent()
            .and_then(Path::file_name)
            .is_some_and(|name| name == "config")
    {
        return root_path;
    }
    root_path.join("config").join("lake")
}

/// Load and validate reference settings with environment precedence.
///
/// `environ` carries the `LAKE_REFERENCE__` overrides; pass the process
/// environment for the production path.
pub fn load_reference_settings(
    root: Option<&Path>,
    environ: &BTreeMap<String, String>,
) -> Result<ReferenceSettings, ReferenceError> {
    let path = reference_config_dir(root).join("reference.yaml");
    let defaults = load_yaml_mapping(&path)?;
    let settings = ReferenceSettings::from_value(&defaults)?;
    Ok(settings.with_environment(environ)?)
}

/// The repository root as resolved at compile time from the crate location.
fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("crate manifest lives under <root>/src/lake/reference")
        .to_path_buf()
}

/// The process environment collected into an override map.
pub fn process_environ() -> BTreeMap<String, String> {
    std::env::vars().collect()
}
