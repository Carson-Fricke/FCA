//! Bounded full-input validation including loading and configuration checks.

use std::collections::VecDeque;
use std::path::Path;
use std::sync::{Mutex, OnceLock};

use backtest::sha256::digest_hex;

use crate::compiler::sdk_dependency_path;
use crate::error::JitError;
use crate::provenance::compiler_fingerprint;
use crate::toolchain::resolve_toolchain;
use crate::workspace::ManagedWorkspace;

/// Structured validation failure with an explicit deterministic cacheability boundary.
#[derive(Clone, Debug)]
pub struct ValidationFailure {
    /// Compact actionable detail; compiler locations and suggestions are in diagnostics.
    pub message: String,
    /// Compact Rustc JSON diagnostics, without rendered copies or code explanations.
    pub diagnostics: Vec<serde_json::Value>,
    /// Whether rustc reported an authored-source error rather than infrastructure failure.
    pub deterministic_compiler_diagnostic: bool,
    /// Whether the service may cache this deterministic failure.
    pub cacheable: bool,
    /// Whether the complete validation result was reused.
    pub reused: bool,
    /// Optional compiler-service provenance for this result.
    pub validation_identity: Option<String>,
}

impl From<JitError> for ValidationFailure {
    fn from(error: JitError) -> Self {
        let diagnostics = match &error {
            JitError::Compile { diagnostics, .. } => diagnostics.clone(),
            _ => Vec::new(),
        };
        let message = match &error {
            JitError::Compile { digest, .. } if !diagnostics.is_empty() => format!(
                "authored program {digest} failed to compile ({} error(s)); fix the reported locations and suggestions, then call validate_study again",
                diagnostics.len()
            ),
            JitError::Export { symbol, .. } => format!(
                "authored artifact is missing required SDK export {symbol:?}. Export the study type at module scope with harness_strategy_sdk::export_study_program!(MyStudy); do not hand-write ABI exports. Minimal template:\n\
                 struct MyStudy;\n\
                 impl harness_strategy_sdk::MarketConsumer for MyStudy {{\n\
                 type Context = harness_strategy_sdk::ObservationContext;\n\
                 fn subscriptions(&self) -> Vec<harness_strategy_sdk::Subscription> {{ vec![harness_strategy_sdk::Subscription::Bars {{ symbols: vec![\"SPY\".into()], interval: \"1d\".into() }}] }}\n\
                 }}\n\
                 impl harness_strategy_sdk::StudyProgram for MyStudy {{\n\
                 fn new(_: harness_strategy_sdk::serde_json::Value) -> Self {{ Self }}\n\
                 }}\n\
                 harness_strategy_sdk::export_study_program!(MyStudy);\n\
                 Adapt subscriptions to harnessConfiguration, add callbacks/metrics, and call validate_study again."
            ),
            _ => error.to_string(),
        };
        Self {
            message,
            deterministic_compiler_diagnostic: !diagnostics.is_empty(),
            cacheable: !diagnostics.is_empty() || matches!(error, JitError::Invalid(_)),
            diagnostics,
            reused: false,
            validation_identity: None,
        }
    }
}

impl ValidationFailure {
    /// Tailor SDK repair instructions to the actual program kind being loaded.
    #[must_use]
    pub fn for_program(error: JitError, kind: crate::compile::ProgramKind) -> Self {
        let mut failure = Self::from(error);
        if kind == crate::compile::ProgramKind::Strategy {
            failure.message = failure
                .message
                .replace("validate_study", "validate_strategy")
                .replace("study type", "strategy type")
                .replace("MyStudy", "MyStrategy")
                .replace("StudyProgram", "StrategyProgram")
                .replace("ObservationContext", "OrderSink")
                .replace("export_study_program", "export_strategy_program");
        }
        failure
    }

    /// Configuration and infrastructure failures are not compiler diagnostics.
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            diagnostics: Vec::new(),
            deterministic_compiler_diagnostic: false,
            cacheable: false,
            reused: false,
            validation_identity: None,
        }
    }

    /// A deterministic authored configuration rejection, not a compiler error.
    #[must_use]
    pub fn configuration(message: impl Into<String>) -> Self {
        Self {
            cacheable: true,
            ..Self::new(message)
        }
    }
}

type ValidationResult = Result<serde_json::Value, ValidationFailure>;
const VALIDATION_CACHE_BYTES: usize = 4 * 1024 * 1024;

fn validation_result_bytes(key: &str, result: &ValidationResult) -> usize {
    key.len()
        + match result {
            Ok(value) => value.to_string().len(),
            Err(error) => {
                error.message.len()
                    + serde_json::to_vec(&error.diagnostics)
                        .map_or(usize::MAX / 2, |bytes| bytes.len())
                    + error.validation_identity.as_ref().map_or(0, String::len)
                    + std::mem::size_of::<ValidationFailure>()
            }
        }
}

/// Bounded cache of complete validation, including loading and configuration checks.
#[derive(Default)]
pub struct ValidationService {
    entries: Mutex<VecDeque<(String, ValidationResult)>>,
}

impl ValidationService {
    /// Version-matched SDK source identity and optional bounded source reference.
    pub fn sdk_reference(
        &self,
        arguments: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<serde_json::Value, JitError> {
        use serde_json::json;
        use std::collections::BTreeMap;

        let sdk = std::path::PathBuf::from(sdk_dependency_path());
        let mut hashes = BTreeMap::new();
        for (name, root) in [
            ("strategy-sdk", sdk.clone()),
            ("contracts", sdk.join("../contracts")),
            ("num", sdk.join("../num")),
            ("lake-contracts", sdk.join("../../lake/contracts")),
        ] {
            hashes.insert(
                format!("{name}/Cargo.toml"),
                digest_hex(&std::fs::read(root.join("Cargo.toml"))?),
            );
            let mut count = 0;
            let mut directories = vec![root.join("src")];
            while let Some(directory) = directories.pop() {
                for entry in std::fs::read_dir(directory)? {
                    let path = entry?.path();
                    if path.is_dir() {
                        directories.push(path);
                    } else if path.is_file()
                        && path.extension().is_some_and(|extension| extension == "rs")
                    {
                        let relative = path
                            .strip_prefix(&root)
                            .map_err(|e| JitError::Invalid(e.to_string()))?;
                        let file = relative
                            .to_str()
                            .ok_or_else(|| {
                                JitError::Invalid("SDK source filename is not UTF-8".into())
                            })?
                            .replace('\\', "/");
                        hashes.insert(format!("{name}/{file}"), digest_hex(&std::fs::read(&path)?));
                        count += 1;
                    }
                }
            }
            if count == 0 {
                return Err(JitError::Invalid(format!(
                    "SDK reference sources missing for {name}"
                )));
            }
        }
        let identity = digest_hex(
            &serde_json::to_vec(&hashes).map_err(|error| JitError::Io(error.to_string()))?,
        );
        let manifest = std::fs::read_to_string(sdk.join("Cargo.toml"))?;
        let abi = std::fs::read_to_string(sdk.join("src/abi.rs"))?;
        let version = manifest
            .split("[package]")
            .nth(1)
            .and_then(|package| package.split('[').next())
            .and_then(|package| {
                package.lines().find_map(|line| {
                    let (key, value) = line.split_once('=')?;
                    (key.trim() == "version").then(|| value.trim().trim_matches('"').to_string())
                })
            })
            .ok_or_else(|| JitError::Invalid("SDK version metadata is malformed".into()))?;
        let abi_version = abi
            .split("SDK_ABI_VERSION: u32 =")
            .nth(1)
            .and_then(|value| value.split(';').next())
            .and_then(|value| value.trim().parse::<u32>().ok())
            .ok_or_else(|| JitError::Invalid("SDK ABI metadata is malformed".into()))?;
        let mut result = json!({
            "sdkIdentity": identity,
            "sdkVersion": version,
            "sdkAbiVersion": abi_version,
            "sourceHashes": hashes,
        });
        if let Ok(identity) = self.validation_identity() {
            result["validationIdentity"] = json!(identity);
        }
        if let Some(topic) = arguments.get("topic") {
            let topic = topic
                .as_str()
                .ok_or_else(|| JitError::Invalid("topic must be a string".into()))?;
            let (file, hash_key) = match topic {
                "consumer" => ("src/consumer.rs", "strategy-sdk/src/consumer.rs"),
                "program" => ("src/program.rs", "strategy-sdk/src/program.rs"),
                "outputs" => ("src/outputs.rs", "strategy-sdk/src/outputs.rs"),
                "research_views" => ("src/research/view.rs", "strategy-sdk/src/research/view.rs"),
                "research_publishing" => (
                    "src/research/publishing.rs",
                    "strategy-sdk/src/research/publishing.rs",
                ),
                "research_recipes" => (
                    "src/research/recipes.rs",
                    "strategy-sdk/src/research/recipes.rs",
                ),
                "building_blocks" => (
                    "src/building_blocks.rs",
                    "strategy-sdk/src/building_blocks.rs",
                ),
                "indicator_api" => ("src/indicator.rs", "strategy-sdk/src/indicator.rs"),
                "wavelet" => ("src/wavelet.rs", "strategy-sdk/src/wavelet.rs"),
                "events" => ("src/events.rs", "strategy-sdk/src/events.rs"),
                "exports" => ("src/export.rs", "strategy-sdk/src/export.rs"),
                "decimal" => ("../num/src/decimal.rs", "num/src/decimal.rs"),
                _ => return Err(JitError::Invalid("unknown SDK reference topic".into())),
            };
            let number = |key: &str, default: u64| -> Result<usize, JitError> {
                let value = match arguments.get(key) {
                    Some(value) => value.as_u64().ok_or_else(|| {
                        JitError::Invalid(format!("{key} must be a positive integer"))
                    })?,
                    None => default,
                };
                usize::try_from(value)
                    .ok()
                    .filter(|value| *value > 0)
                    .ok_or_else(|| JitError::Invalid(format!("{key} must be a positive integer")))
            };
            let start = number("start_line", 1)?;
            let max = number("max_chars", 16000)?;
            if max > 16000 {
                return Err(JitError::Invalid("max_chars must be at most 16000".into()));
            }
            let text = std::fs::read_to_string(sdk.join(file))?;
            if digest_hex(text.as_bytes()) != hashes[hash_key] {
                return Err(JitError::Invalid(
                    "SDK reference changed while reading; request again".into(),
                ));
            }
            let lines: Vec<_> = text.lines().collect();
            let end = number("end_line", lines.len() as u64)?;
            if start > end || end > lines.len() {
                return Err(JitError::Invalid(
                    "SDK line range is outside the source".into(),
                ));
            }
            let selected = lines[start - 1..end].join("\n");
            let content: String = selected.chars().take(max).collect();
            result["topic"] = json!(topic);
            result["content"] = json!(content);
            result["startLine"] = json!(start);
            result["endLine"] = json!(end);
            result["totalLines"] = json!(lines.len());
            result["truncated"] = json!(selected.chars().count() > max);
        }
        Ok(result)
    }

    /// The compiler service's opaque provenance. Failure disables result reuse.
    pub fn validation_identity(&self) -> Result<String, JitError> {
        let workspace = ManagedWorkspace::open_default()?;
        let toolchain = resolve_toolchain(workspace.root())?;
        compiler_fingerprint(
            workspace.root(),
            Path::new(&sdk_dependency_path()),
            &toolchain.digest,
        )
        .map(|identity| format!("sha256:{identity}"))
    }

    /// Cache full-input successes and deterministic errors, never infrastructure failures.
    pub fn validate(
        &self,
        model: &serde_json::Value,
        inspect: impl FnOnce() -> ValidationResult,
    ) -> ValidationResult {
        let identity = self.validation_identity().ok();
        self.validate_cached(model, identity, inspect, || self.validation_identity().ok())
    }

    fn validate_cached(
        &self,
        model: &serde_json::Value,
        identity: Option<String>,
        inspect: impl FnOnce() -> ValidationResult,
        current_identity: impl FnOnce() -> Option<String>,
    ) -> ValidationResult {
        let key = identity
            .as_ref()
            .map(|identity| format!("{identity}:{}", digest_hex(model.to_string().as_bytes())));
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| ValidationFailure::new("validation result cache poisoned"))?;
        if let Some(key) = &key {
            if let Some((_, result)) = entries.iter().find(|(stored, _)| stored == key) {
                let mut result = result.clone();
                match &mut result {
                    Ok(value) => value["reused"] = serde_json::json!(true),
                    Err(error) => error.reused = true,
                }
                return result;
            }
        }
        let mut result = inspect();
        if let Ok(value) = &mut result {
            value["reused"] = serde_json::json!(false);
            value["deterministicCompilerDiagnostic"] = serde_json::json!(false);
            value["cacheable"] = serde_json::json!(identity.is_some());
            if let Some(identity) = &identity {
                value["validationIdentity"] = serde_json::json!(identity);
            }
        }
        if let Err(error) = &mut result {
            error.validation_identity = identity.clone();
            error.cacheable &= identity.is_some();
        }
        if let Some(key) = key {
            // Recheck after inspection so changed SDK/config cannot populate a stale key.
            let stable = current_identity() == identity;
            if !stable {
                match &mut result {
                    Ok(value) => {
                        value["cacheable"] = serde_json::json!(false);
                        value.as_object_mut().unwrap().remove("validationIdentity");
                    }
                    Err(error) => {
                        error.cacheable = false;
                        error.validation_identity = None;
                    }
                }
            }
            if stable
                && (result.is_ok() || result.as_ref().err().is_some_and(|error| error.cacheable))
            {
                let bytes = validation_result_bytes(&key, &result);
                if bytes <= VALIDATION_CACHE_BYTES {
                    let mut retained: usize = entries
                        .iter()
                        .map(|(key, result)| validation_result_bytes(key, result))
                        .sum();
                    while entries.len() >= 64 || retained + bytes > VALIDATION_CACHE_BYTES {
                        if let Some((key, result)) = entries.pop_front() {
                            retained -= validation_result_bytes(&key, &result);
                        }
                    }
                    entries.push_back((key, result.clone()));
                }
            }
        }
        result
    }
}

/// Shared validator service used by local adapters in the current process.
pub fn validation_service() -> &'static ValidationService {
    static SERVICE: OnceLock<ValidationService> = OnceLock::new();
    SERVICE.get_or_init(ValidationService::default)
}

#[cfg(test)]
#[path = "../tests/unit/validation.rs"]
mod tests;
