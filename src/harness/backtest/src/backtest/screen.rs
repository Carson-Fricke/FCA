//! Typed screening configuration for the frozen `shell: screen` document.
//!
//! This is the configuration Thesis materializes per run
//! (`ScreenerDefinition.materialize_config`): the authored Rust source plus its
//! `bundle_hash`, the as-of date and window, the universe revision, the rank
//! metric, and the definition provenance. Two documents that differ in any
//! field produce different content-addressed run identifiers, so a run is
//! named by its inputs alone — no wall clock, no randomness.
//!
//! Unknown keys are rejected. `bundle_hash` must be the SHA-256 of the
//! LF-normalized authored source, and `source_sha256` (when present) must
//! agree; a mismatch fails before any JIT work starts.

use std::collections::BTreeMap;
use std::path::Path;

use crate::backtest::errors::BacktestError;
use crate::backtest::fixture::{date_string, days_from_civil};
use crate::backtest::yaml::{load_yaml, YamlValue};
use crate::sha256::digest_hex;

/// The configuration shell every screening document declares.
pub const SCREEN_SHELL: &str = "screen";

/// The domain separator for the screening configuration content address.
const SCREEN_CONFIG_DOMAIN: &[u8] = b"fca:harness-screen-config:v1\0";

/// Every top-level key a screening configuration may carry.
const SCREEN_KEYS: [&str; 16] = [
    "schema_version",
    "shell",
    "job_id",
    "name",
    "as_of",
    "bar_intervals",
    "lookback_days",
    "rank_metric",
    "universe_revision",
    "source",
    "source_path",
    "source_sha256",
    "bundle_hash",
    "definition_id",
    "definition_hash",
    "parameters",
];

/// A validated `shell: screen` configuration.
///
/// `source` and `bundle_hash` are the JIT seam's contract: the source is
/// already LF-normalized and `bundle_hash` is its exact SHA-256, verified at
/// parse time. `definition_id` and `definition_hash` are Thesis provenance and
/// are echoed into the `screen.json` artifact unchanged.
#[derive(Clone, Debug, PartialEq)]
pub struct ScreenConfig {
    /// Schema version (always `1`).
    pub schema_version: u64,
    /// Configuration shell (always `screen`).
    pub shell: String,
    /// Execution job identifier.
    pub job_id: String,
    /// Human-readable screener name.
    pub name: String,
    /// Evaluation date (`YYYY-MM-DD`).
    pub as_of: String,
    /// Canonical requested minute/hour/session cadences.
    pub bar_intervals: Vec<String>,
    /// Calendar days of history ending at `as_of`.
    pub lookback_days: u32,
    /// Metric the host ranks rows by.
    pub rank_metric: String,
    /// Universe revision identifier, when the definition names one.
    pub universe_revision: Option<String>,
    /// LF-normalized authored Rust source.
    pub source: String,
    /// The authored-source SHA-256, equal to `bundle_hash`.
    pub source_sha256: String,
    /// SHA-256 of the LF-normalized authored source.
    pub bundle_hash: String,
    /// Thesis definition identity (`sha256:<hex>`).
    pub definition_id: String,
    /// Thesis definition content hash (64 lowercase hex).
    pub definition_hash: String,
    /// Per-definition JSON parameters passed to the authored program.
    pub parameters: serde_json::Value,
}

impl ScreenConfig {
    /// Parse a screening configuration from a YAML document.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] on any malformed document,
    /// unknown key, or bundle-hash mismatch. `source_path` cannot be resolved
    /// from text alone; use [`load_screen_config`](crate::backtest::job::load_screen_config)
    /// for documents that reference the source by path.
    pub fn from_yaml(text: &str) -> Result<Self, BacktestError> {
        let value = load_yaml(text).map_err(|error| BacktestError::Configuration(error.0))?;
        Self::from_yaml_value_with_base(&value, None)
    }

    /// Parse a screening configuration from a parsed YAML mapping.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] under the same conditions as
    /// [`ScreenConfig::from_yaml`].
    pub fn from_yaml_value(value: &YamlValue) -> Result<Self, BacktestError> {
        Self::from_yaml_value_with_base(value, None)
    }

    /// Parse a screening configuration from a parsed YAML mapping, resolving
    /// `source_path` relative to `base_dir`.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] under the same conditions as
    /// [`ScreenConfig::from_yaml`].
    pub fn from_yaml_value_with_base(
        value: &YamlValue,
        base_dir: Option<&Path>,
    ) -> Result<Self, BacktestError> {
        let root = value.as_mapping().ok_or_else(|| {
            BacktestError::Configuration("configuration root must be a mapping".to_string())
        })?;
        reject_unknown_keys(root.keys())?;
        let fields = RawFields {
            schema_version: required_int(root, "schema_version")?,
            shell: required_string(root, "shell")?,
            job_id: required_string(root, "job_id")?,
            name: required_string(root, "name")?,
            as_of: required_string(root, "as_of")?,
            bar_intervals: root
                .get("bar_intervals")
                .and_then(YamlValue::as_sequence)
                .ok_or_else(|| configuration_error("bar_intervals must be a list"))?
                .iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_string)
                        .ok_or_else(|| configuration_error("bar_intervals must contain strings"))
                })
                .collect::<Result<Vec<_>, _>>()?,
            lookback_days: required_int(root, "lookback_days")?,
            rank_metric: required_string(root, "rank_metric")?,
            universe_revision: optional_string(root, "universe_revision")?,
            source: optional_string(root, "source")?,
            source_path: optional_string(root, "source_path")?,
            source_sha256: optional_string(root, "source_sha256")?,
            bundle_hash: required_string(root, "bundle_hash")?,
            definition_id: required_string(root, "definition_id")?,
            definition_hash: required_string(root, "definition_hash")?,
            parameters: yaml_parameters(root.get("parameters"))?,
        };
        assemble(fields, base_dir)
    }

    /// Parse a screening configuration from a JSON document.
    ///
    /// This is the frozen wire shape Thesis sends: JSON keys map one-to-one to
    /// the YAML fields, and `parameters` stays exactly the JSON value the
    /// definition supplied.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] under the same conditions as
    /// [`ScreenConfig::from_yaml`]; a `source_path` cannot be resolved from a
    /// standalone JSON document.
    pub fn from_json_value(document: &serde_json::Value) -> Result<Self, BacktestError> {
        Self::from_json_value_with_base(document, None)
    }

    /// Parse a screening configuration from a JSON document, resolving
    /// `source_path` relative to `base_dir`.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] under the same conditions as
    /// [`ScreenConfig::from_json_value`].
    pub fn from_json_value_with_base(
        document: &serde_json::Value,
        base_dir: Option<&Path>,
    ) -> Result<Self, BacktestError> {
        let object = document.as_object().ok_or_else(|| {
            BacktestError::Configuration("configuration root must be a mapping".to_string())
        })?;
        reject_unknown_keys(object.keys())?;
        let fields = RawFields {
            schema_version: json_int(object, "schema_version")?,
            shell: json_string(object, "shell")?,
            job_id: json_string(object, "job_id")?,
            name: json_string(object, "name")?,
            as_of: json_string(object, "as_of")?,
            bar_intervals: object
                .get("bar_intervals")
                .and_then(serde_json::Value::as_array)
                .ok_or_else(|| configuration_error("bar_intervals must be a list"))?
                .iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_string)
                        .ok_or_else(|| configuration_error("bar_intervals must contain strings"))
                })
                .collect::<Result<Vec<_>, _>>()?,
            lookback_days: json_int(object, "lookback_days")?,
            rank_metric: json_string(object, "rank_metric")?,
            universe_revision: json_optional_string(object, "universe_revision")?,
            source: json_optional_string(object, "source")?,
            source_path: json_optional_string(object, "source_path")?,
            source_sha256: json_optional_string(object, "source_sha256")?,
            bundle_hash: json_string(object, "bundle_hash")?,
            definition_id: json_string(object, "definition_id")?,
            definition_hash: json_string(object, "definition_hash")?,
            parameters: json_parameters(object.get("parameters"))?,
        };
        assemble(fields, base_dir)
    }

    /// The SHA-256 of the LF-normalized authored source.
    #[must_use]
    pub fn source_digest(&self) -> String {
        digest_hex(self.source.as_bytes())
    }

    /// The configuration content address, as 64 lowercase hex.
    ///
    /// The digest is domain-separated and canonical:
    /// `sha256("fca:harness-screen-config:v1\0" || canonical_json(content))`,
    /// where `content` carries every run-defining field (the authored source
    /// enters through `bundle_hash`). Object keys are sorted, so the digest is
    /// stable across processes and checkout line endings. The execution
    /// `job_id` is provenance-excluded so identical configurations always
    /// produce the same digest.
    #[must_use]
    pub fn configuration_digest(&self) -> String {
        let payload =
            serde_json::to_string(&self.content_document()).unwrap_or_else(|_| String::new());
        let mut bytes = Vec::with_capacity(SCREEN_CONFIG_DOMAIN.len() + payload.len());
        bytes.extend_from_slice(SCREEN_CONFIG_DOMAIN);
        bytes.extend_from_slice(payload.as_bytes());
        digest_hex(&bytes)
    }

    /// The first session of the run's data window (`YYYY-MM-DD`).
    ///
    /// Defined as `as_of` minus `lookback_days` calendar days; the window end
    /// is `as_of` itself.
    #[must_use]
    pub fn window_start(&self) -> String {
        let days = parse_date_days(&self.as_of).unwrap_or(0);
        date_string(days - i64::from(self.lookback_days))
    }

    /// The last session of the run's data window (`YYYY-MM-DD`), equal to
    /// [`as_of`](ScreenConfig::as_of).
    #[must_use]
    pub fn window_end(&self) -> String {
        self.as_of.clone()
    }

    fn content_document(&self) -> serde_json::Value {
        serde_json::json!({
            "as_of": self.as_of,
            "bar_intervals": self.bar_intervals,
            "bundle_hash": self.bundle_hash,
            "definition_hash": self.definition_hash,
            "definition_id": self.definition_id,
            "lookback_days": self.lookback_days,
            "name": self.name,
            "parameters": self.parameters,
            "rank_metric": self.rank_metric,
            "schema_version": self.schema_version,
            "shell": self.shell,
            "universe_revision": self.universe_revision,
        })
    }
}

/// One raw field set shared by the YAML and JSON parse paths.
struct RawFields {
    schema_version: i64,
    shell: String,
    job_id: String,
    name: String,
    as_of: String,
    bar_intervals: Vec<String>,
    lookback_days: i64,
    rank_metric: String,
    universe_revision: Option<String>,
    source: Option<String>,
    source_path: Option<String>,
    source_sha256: Option<String>,
    bundle_hash: String,
    definition_id: String,
    definition_hash: String,
    parameters: serde_json::Value,
}

fn assemble(fields: RawFields, base_dir: Option<&Path>) -> Result<ScreenConfig, BacktestError> {
    if fields.schema_version != 1 {
        return Err(configuration_error("schema_version must be 1"));
    }
    if fields.shell != SCREEN_SHELL {
        return Err(configuration_error(&format!(
            "configuration shell must be '{SCREEN_SHELL}', not '{}'",
            fields.shell
        )));
    }
    let job_id = validated_job_id(&fields.job_id)?;
    let name = fields.name.trim();
    if name.is_empty() || name.len() > 256 {
        return Err(configuration_error("name must be 1 to 256 characters"));
    }
    parse_date_days(&fields.as_of)
        .ok_or_else(|| configuration_error("as_of must be a YYYY-MM-DD session date"))?;
    if fields.bar_intervals.is_empty() {
        return Err(configuration_error("bar_intervals must be nonempty"));
    }
    for interval in &fields.bar_intervals {
        super::engine_core::replay_interval_ns(interval)?;
    }
    let mut intervals = fields.bar_intervals.clone();
    intervals.sort();
    intervals.dedup();
    if fields.lookback_days <= 0 {
        return Err(configuration_error(
            "lookback_days must be a positive integer",
        ));
    }
    if fields.rank_metric.trim().is_empty() {
        return Err(configuration_error(
            "rank_metric must be a non-empty string",
        ));
    }
    if let Some(revision) = &fields.universe_revision {
        if revision.trim().is_empty() {
            return Err(configuration_error(
                "universe_revision must be a non-empty string when present",
            ));
        }
    }
    let source = resolve_source(&fields, base_dir)?;
    if source.trim().is_empty() {
        return Err(configuration_error("source must not be empty"));
    }
    let digest = digest_hex(source.as_bytes());
    let bundle_hash = lowercase_hex(&fields.bundle_hash, "bundle_hash")?;
    if bundle_hash != digest {
        return Err(configuration_error(&format!(
            "bundle_hash {bundle_hash:?} does not match the authored source digest {digest}"
        )));
    }
    let source_sha256 = match &fields.source_sha256 {
        Some(declared) => {
            let declared = lowercase_hex(declared, "source_sha256")?;
            if declared != digest {
                return Err(configuration_error(&format!(
                    "source_sha256 {declared:?} does not match the authored source digest {digest}"
                )));
            }
            declared
        }
        None => digest.clone(),
    };
    let definition_hash = lowercase_hex(&fields.definition_hash, "definition_hash")?;
    let definition_id = fields.definition_id.trim();
    let Some(definition_hex) = definition_id.strip_prefix("sha256:") else {
        return Err(configuration_error(
            "definition_id must use the sha256:<hex> form",
        ));
    };
    if lowercase_hex(definition_hex, "definition_id").is_err() {
        return Err(configuration_error(
            "definition_id must use the sha256:<hex> form",
        ));
    }
    Ok(ScreenConfig {
        schema_version: 1,
        shell: SCREEN_SHELL.to_string(),
        job_id,
        name: name.to_string(),
        as_of: fields.as_of,
        bar_intervals: intervals,
        lookback_days: u32::try_from(fields.lookback_days)
            .map_err(|_| configuration_error("lookback_days is out of range"))?,
        rank_metric: fields.rank_metric,
        universe_revision: fields.universe_revision,
        source,
        source_sha256,
        bundle_hash,
        definition_id: format!("sha256:{definition_hex}"),
        definition_hash,
        parameters: fields.parameters,
    })
}

fn resolve_source(fields: &RawFields, base_dir: Option<&Path>) -> Result<String, BacktestError> {
    if let Some(source) = &fields.source {
        return Ok(normalize_source(source));
    }
    let Some(relative) = &fields.source_path else {
        return Err(configuration_error(
            "screen configuration requires 'source' or 'source_path'",
        ));
    };
    let base_dir = base_dir.ok_or_else(|| {
        configuration_error(
            "source_path requires a configuration directory; load through load_screen_config",
        )
    })?;
    let resolved = base_dir.join(relative);
    let text = std::fs::read_to_string(&resolved).map_err(|error| {
        configuration_error(&format!(
            "authored source {} could not be read: {error}",
            resolved.display()
        ))
    })?;
    Ok(normalize_source(&text))
}

/// Normalize CRLF and lone CR to LF, matching the Thesis definition digest.
fn normalize_source(source: &str) -> String {
    source.replace("\r\n", "\n").replace('\r', "\n")
}

fn parse_date_days(text: &str) -> Option<i64> {
    let bytes = text.as_bytes();
    if bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    if !bytes
        .iter()
        .enumerate()
        .all(|(index, byte)| index == 4 || index == 7 || byte.is_ascii_digit())
    {
        return None;
    }
    let year: i64 = text[..4].parse().ok()?;
    let month: i64 = text[5..7].parse().ok()?;
    let day: i64 = text[8..10].parse().ok()?;
    if !(1..=12).contains(&month) {
        return None;
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let max_day = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    if !(1..=max_day).contains(&day) {
        return None;
    }
    Some(days_from_civil(year, month, day))
}

fn reject_unknown_keys<'a>(keys: impl Iterator<Item = &'a String>) -> Result<(), BacktestError> {
    for key in keys {
        if !SCREEN_KEYS.contains(&key.as_str()) {
            return Err(configuration_error(&format!(
                "unexpected configuration field '{key}'"
            )));
        }
    }
    Ok(())
}

fn configuration_error(message: &str) -> BacktestError {
    BacktestError::Configuration(message.to_string())
}

fn validated_job_id(value: &str) -> Result<String, BacktestError> {
    let trimmed = value.trim();
    if trimmed.is_empty()
        || trimmed.len() > 128
        || trimmed.contains('/')
        || trimmed.contains('\\')
        || trimmed == "."
        || trimmed == ".."
    {
        return Err(configuration_error(
            "job_id must be a single safe path segment of at most 128 characters",
        ));
    }
    Ok(trimmed.to_string())
}

fn lowercase_hex(value: &str, field: &str) -> Result<String, BacktestError> {
    if value.len() == 64
        && value
            .chars()
            .all(|character| character.is_ascii_hexdigit() && !character.is_ascii_uppercase())
    {
        return Ok(value.to_string());
    }
    Err(configuration_error(&format!(
        "{field} must be a 64-character lowercase hex digest"
    )))
}

fn required_string(
    mapping: &BTreeMap<String, YamlValue>,
    key: &str,
) -> Result<String, BacktestError> {
    mapping
        .get(key)
        .and_then(YamlValue::as_str)
        .map(str::to_string)
        .ok_or_else(|| configuration_error(&format!("missing string field '{key}'")))
}

fn optional_string(
    mapping: &BTreeMap<String, YamlValue>,
    key: &str,
) -> Result<Option<String>, BacktestError> {
    match mapping.get(key) {
        None | Some(YamlValue::Null) => Ok(None),
        Some(YamlValue::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(configuration_error(&format!(
            "field '{key}' must be a string"
        ))),
    }
}

fn required_int(mapping: &BTreeMap<String, YamlValue>, key: &str) -> Result<i64, BacktestError> {
    mapping
        .get(key)
        .and_then(YamlValue::as_int)
        .ok_or_else(|| configuration_error(&format!("missing integer field '{key}'")))
}

fn yaml_parameters(value: Option<&YamlValue>) -> Result<serde_json::Value, BacktestError> {
    match value {
        None | Some(YamlValue::Null) => Ok(serde_json::json!({})),
        Some(YamlValue::Mapping(entries)) => {
            let mut object = serde_json::Map::new();
            for (key, item) in entries {
                object.insert(key.clone(), yaml_to_json(item)?);
            }
            Ok(serde_json::Value::Object(object))
        }
        Some(_) => Err(configuration_error(
            "parameters must be a mapping when present",
        )),
    }
}

fn yaml_to_json(value: &YamlValue) -> Result<serde_json::Value, BacktestError> {
    Ok(match value {
        YamlValue::Null => serde_json::Value::Null,
        YamlValue::Bool(flag) => serde_json::Value::Bool(*flag),
        YamlValue::Int(number) => serde_json::Value::from(*number),
        // JSON has no exact decimal type; rendering the fixed-point text keeps
        // the value lossless instead of rounding through a float.
        YamlValue::Decimal(number) => {
            serde_json::Value::String(crate::backtest::config::py_decimal_fixed(*number))
        }
        YamlValue::String(text) => serde_json::Value::String(text.clone()),
        YamlValue::Sequence(items) => serde_json::Value::Array(
            items
                .iter()
                .map(yaml_to_json)
                .collect::<Result<Vec<_>, _>>()?,
        ),
        YamlValue::Mapping(entries) => serde_json::Value::Object(
            entries
                .iter()
                .map(|(key, item)| Ok((key.clone(), yaml_to_json(item)?)))
                .collect::<Result<serde_json::Map<String, serde_json::Value>, BacktestError>>()?,
        ),
    })
}

fn json_string(
    object: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<String, BacktestError> {
    object
        .get(key)
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| configuration_error(&format!("missing string field '{key}'")))
}

fn json_optional_string(
    object: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<Option<String>, BacktestError> {
    match object.get(key) {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(configuration_error(&format!(
            "field '{key}' must be a string"
        ))),
    }
}

fn json_int(
    object: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<i64, BacktestError> {
    object
        .get(key)
        .and_then(serde_json::Value::as_i64)
        .ok_or_else(|| configuration_error(&format!("missing integer field '{key}'")))
}

fn json_parameters(value: Option<&serde_json::Value>) -> Result<serde_json::Value, BacktestError> {
    match value {
        None | Some(serde_json::Value::Null) => Ok(serde_json::json!({})),
        Some(serde_json::Value::Object(entries)) => Ok(serde_json::Value::Object(entries.clone())),
        Some(_) => Err(configuration_error(
            "parameters must be a mapping when present",
        )),
    }
}
