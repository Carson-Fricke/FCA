//! Owns loading and freezing current research documents and authored sources.
//! Shared schemas validate behavior; this module neither acquires data nor executes programs.
use std::path::Path;

use crate::error::BacktestJobError;
use backtest::backtest::config::BacktestConfig;
use backtest::backtest::screen::ScreenConfig;
use harness_contracts::canonical_json::JsonValue;
use harness_trading::replay_errors::BacktestError;

/// Read the top-level `shell` scalar from a configuration document.
#[must_use]
pub fn parse_shell(text: &str) -> Option<String> {
    let mapping = config::replay::parse_document(text).ok()?;
    match mapping.get("shell") {
        Some(config::YamlValue::Str(shell)) if !shell.is_empty() => Some(shell.clone()),
        _ => None,
    }
}

/// Read a top-level scalar (string or integer) from a configuration document.
#[must_use]
pub fn parse_scalar(text: &str, key: &str) -> Option<String> {
    let mapping = config::replay::parse_document(text).ok()?;
    match mapping.get(key)? {
        config::YamlValue::Str(v) if !v.is_empty() => Some(v.clone()),
        config::YamlValue::Int(v) => Some(v.to_string()),
        _ => None,
    }
}

/// Read a scalar one indent level below a top-level `section:` block.
#[must_use]
pub fn parse_nested_scalar(text: &str, section: &str, key: &str) -> Option<String> {
    let mapping = config::replay::parse_document(text).ok()?;
    let config::YamlValue::Map(section) = mapping.get(section)? else {
        return None;
    };
    match section.get(key)? {
        config::YamlValue::Str(v) if !v.is_empty() => Some(v.clone()),
        config::YamlValue::Int(v) => Some(v.to_string()),
        _ => None,
    }
}

/// The fixed identity used only for authored-program validation documents.
pub const VALIDATION_JOB_ID: &str = "authored-validation";

/// Load and validate the resolved fields one backtest job reads.
///
/// The full Python loader in `harness.configuration` validates the whole schema
/// graph; this reads the same typed fields the staged runner consumes: the
/// shell, run window, seed, accounting settings, and data identity.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document is missing a `backtest`
/// shell, carries an invalid window, or cannot be read.
pub fn load_backtest_config(config_path: &Path) -> Result<BacktestConfig, BacktestJobError> {
    load_backtest_config_with_job_id(config_path, None)
}

/// Load one backtest configuration with an explicit submission `job_id`.
///
/// The submitted `job_id` is applied to the raw document before validation;
/// it replaces any value already present because one authored document can
/// serve many executions.
///
/// # Errors
///
/// Returns [`BacktestJobError`] under the same conditions as
/// [`load_backtest_config`], and for an invalid submitted `job_id`.
pub fn load_backtest_config_with_job_id(
    config_path: &Path,
    job_id: Option<&str>,
) -> Result<BacktestConfig, BacktestJobError> {
    load_replay_submission(config_path, job_id, "backtest").map(|(configuration, _)| configuration)
}

/// Load and validate one study configuration document (shell `study`).
///
/// A study reuses the replay configuration shape but declares the `study`
/// shell, so it is never confused with a backtest. The strategy-instance list
/// names platform study programs rather than user strategies, and the raw
/// document declares exact `data.subscriptions`. Tick-only inputs need no
/// interval.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document is missing a `study` shell,
/// omits `data.subscriptions`, carries an invalid window, or cannot be read.
pub fn load_study_config(config_path: &Path) -> Result<BacktestConfig, BacktestJobError> {
    load_study_config_with_job_id(config_path, None)
}

/// Load one study configuration with an explicit submission `job_id`.
///
/// # Errors
///
/// Returns [`BacktestJobError`] under the same conditions as
/// [`load_study_config`], and for an invalid submitted `job_id`.
pub fn load_study_config_with_job_id(
    config_path: &Path,
    job_id: Option<&str>,
) -> Result<BacktestConfig, BacktestJobError> {
    load_replay_submission(config_path, job_id, "study").map(|(configuration, _)| configuration)
}

/// Capture the admitted document and resolved replay configuration from one read.
///
/// # Errors
/// Returns a configuration error before admission for invalid shell, id or source.
pub fn load_replay_submission(
    config_path: &Path,
    job_id: Option<&str>,
    expected_shell: &str,
) -> Result<(BacktestConfig, serde_json::Value), BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != expected_shell {
        return Err(BacktestJobError::WrongShell(shell));
    }
    let mut document = config::replay::submitted_document(&text)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    overlay_job_id_json(&mut document, job_id)?;
    let configuration = resolve_authored_sources(
        BacktestConfig::from_yaml(&document.to_string()).map_err(BacktestJobError::from)?,
        config_path,
    )?;
    document["job_id"] = serde_json::Value::String(configuration.job_id.clone());
    if let Some(instances) = document
        .get_mut("strategy_instances")
        .and_then(serde_json::Value::as_array_mut)
    {
        for (document, instance) in instances.iter_mut().zip(&configuration.strategy_instances) {
            if let Some(source) = instance.parameters.get("source") {
                let source =
                    serde_json::from_str::<serde_json::Value>(&source.to_canonical_string())
                        .map_err(|e| BacktestJobError::Io(e.to_string()))?;
                document["parameters"]["source"] = source;
                if let Some(parameters) = document
                    .get_mut("parameters")
                    .and_then(serde_json::Value::as_object_mut)
                {
                    parameters.remove("source_path");
                }
            }
        }
    }
    Ok((configuration, document))
}

/// Load and validate one screening configuration document (shell `screen`).
///
/// The frozen wire shape carries the authored Rust source (or a
/// `source_path` resolved relative to the document) plus its `bundle_hash`,
/// and the definition provenance Thesis attaches to the run. A document whose
/// shell is not `screen`, whose keys are unknown, or whose `bundle_hash` does
/// not match the LF-normalized source fails before any JIT work starts.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document is missing a `screen` shell,
/// carries an invalid or mismatched configuration, or cannot be read.
pub fn load_screen_config(config_path: &Path) -> Result<ScreenConfig, BacktestJobError> {
    load_screen_config_with_job_id(config_path, None)
}

/// Load one screening configuration with an explicit submission `job_id`.
///
/// # Errors
///
/// Returns [`BacktestJobError`] under the same conditions as
/// [`load_screen_config`], and for an invalid submitted `job_id`.
pub fn load_screen_config_with_job_id(
    config_path: &Path,
    job_id: Option<&str>,
) -> Result<ScreenConfig, BacktestJobError> {
    let text = std::fs::read_to_string(config_path)
        .map_err(|error| BacktestJobError::Io(error.to_string()))?;
    let shell = parse_shell(&text).ok_or(BacktestJobError::MissingShell)?;
    if shell != "screen" {
        return Err(BacktestJobError::WrongScreenShell(shell));
    }
    let base_dir = config_path.parent();
    if text.trim_start().starts_with('{') {
        let mut document: serde_json::Value =
            serde_json::from_str(&text).map_err(|error| BacktestJobError::Io(error.to_string()))?;
        overlay_job_id_json(&mut document, job_id)?;
        return ScreenConfig::from_json_value_with_base(&document, base_dir)
            .map_err(BacktestJobError::from);
    }
    let mut value = backtest::backtest::yaml::load_yaml(&text)
        .map_err(|error| BacktestJobError::from(BacktestError::Configuration(error.0)))?;
    overlay_job_id_yaml(&mut value, job_id)?;
    ScreenConfig::from_yaml_value_with_base(&value, base_dir).map_err(BacktestJobError::from)
}

/// Apply an explicit submission `job_id` to a parsed JSON configuration.
///
/// The submission argument wins and replaces any value already present.
///
/// # Errors
///
/// Returns [`BacktestJobError::Engine`] for an invalid submitted `job_id` or a
/// non-mapping document.
pub fn overlay_job_id_json(
    document: &mut serde_json::Value,
    job_id: Option<&str>,
) -> Result<bool, BacktestJobError> {
    let Some(job_id) = job_id else {
        return Ok(false);
    };
    let job_id = validated_submitted_job_id(job_id)?;
    let object = document.as_object_mut().ok_or_else(|| {
        BacktestJobError::Engine("configuration root must be a mapping".to_string())
    })?;
    object.insert("job_id".to_string(), serde_json::Value::String(job_id));
    Ok(true)
}

/// Apply an explicit submission `job_id` to a parsed YAML configuration.
///
/// The submission argument wins and replaces any value already present.
///
/// # Errors
///
/// Returns [`BacktestJobError::Engine`] under the same conditions as
/// [`overlay_job_id_json`].
pub fn overlay_job_id_yaml(
    document: &mut backtest::backtest::yaml::YamlValue,
    job_id: Option<&str>,
) -> Result<bool, BacktestJobError> {
    let Some(job_id) = job_id else {
        return Ok(false);
    };
    let job_id = validated_submitted_job_id(job_id)?;
    let backtest::backtest::yaml::YamlValue::Mapping(mapping) = document else {
        return Err(BacktestJobError::Engine(
            "configuration root must be a mapping".to_string(),
        ));
    };
    mapping.insert(
        "job_id".to_string(),
        backtest::backtest::yaml::YamlValue::String(job_id),
    );
    Ok(true)
}

pub(super) fn validated_submitted_job_id(value: &str) -> Result<String, BacktestJobError> {
    let trimmed = value.trim();
    if trimmed.is_empty()
        || trimmed.len() > 128
        || trimmed.contains('/')
        || trimmed.contains('\\')
        || trimmed == "."
        || trimmed == ".."
    {
        return Err(BacktestJobError::Engine(
            "job_id must be a single safe path segment of at most 128 characters".to_string(),
        ));
    }
    Ok(trimmed.to_string())
}

/// Parse one in-memory study configuration document (JSON tree, `study` shell).
///
/// The harness builds this document from an authored model before compiling
/// it, so parsing reuses the exact typed path [`load_study_config`] uses.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when the document has no `study` shell or the
/// typed configuration does not parse.
pub fn parse_study_document(
    document: &serde_json::Value,
) -> Result<BacktestConfig, BacktestJobError> {
    let shell = document
        .get("shell")
        .and_then(serde_json::Value::as_str)
        .ok_or(BacktestJobError::MissingShell)?;
    if shell != "study" {
        return Err(BacktestJobError::WrongShell(shell.to_string()));
    }
    let configuration =
        BacktestConfig::from_yaml(&document.to_string()).map_err(BacktestJobError::from)?;
    Ok(configuration)
}

/// Inline `parameters.source_path` authored sources after parsing.
///
/// The repository's non-executable YAML subset cannot carry multi-line
/// authored sources, so a YAML configuration references the authored file by
/// path, resolved relative to the configuration document's directory. JSON
/// documents may carry the source inline and need no resolution.
///
/// # Errors
///
/// Returns [`BacktestJobError`] when a `source_path` parameter is not a
/// string or the referenced file cannot be read.
pub(super) fn resolve_authored_sources(
    mut configuration: BacktestConfig,
    config_path: &Path,
) -> Result<BacktestConfig, BacktestJobError> {
    for instance in &mut configuration.strategy_instances {
        if instance.parameters.contains_key("source") {
            continue;
        }
        let Some(JsonValue::String(relative)) = instance.parameters.get("source_path") else {
            continue;
        };
        let resolved = config_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(relative.as_str());
        let source = std::fs::read_to_string(&resolved).map_err(|error| {
            BacktestJobError::Io(format!(
                "strategy instance {:?} authored source {} could not be read: {error}",
                instance.instance_id,
                resolved.display()
            ))
        })?;
        instance
            .parameters
            .insert("source".to_string(), JsonValue::String(source));
        instance.parameters.remove("source_path");
    }
    Ok(configuration)
}
