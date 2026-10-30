//! Building engine instances from harness configurations.
//!
//! A harness configuration runs only JIT-authored Rust programs: every
//! strategy instance names `implementation: "jit"` and carries the authored
//! source in its `parameters` under the `source` key. The source's SHA-256
//! digest must equal the instance's `bundle_hash`, so the existing
//! bundle-hash provenance flow pins exactly what ran.

use backtest::backtest::config::BacktestConfig;
use backtest::backtest::config::StrategyInstanceConfig;
use harness_contracts::canonical_json::JsonValue;
use harness_trading::program::StrategyInstance;

use harness_strategy_jit::strategy_instance;
use harness_strategy_jit::study_instance;
use harness_strategy_jit::JitError;
use harness_strategy_jit::ManagedWorkspace;
use harness_strategy_jit::ProgramKind;

/// The implementation value every harness program instance must use.
pub const JIT_IMPLEMENTATION: &str = "jit";

/// The parameters key carrying the authored Rust source.
pub const SOURCE_PARAMETER: &str = "source";

/// Build the engine instances one configuration runs, JIT-compiling every
/// authored program into `workspace`.
///
/// The program kind follows the configuration shell: `study` shells run
/// study programs (no order surface), everything else runs strategy
/// programs.
///
/// # Errors
///
/// Returns [`JitError`] when an instance does not name the `jit`
/// implementation, omits or malforms its authored source, carries a
/// mismatched `bundle_hash`, or fails to compile, load, or construct.
pub fn build_instances(
    configuration: &BacktestConfig,
    workspace: &ManagedWorkspace,
) -> Result<Vec<StrategyInstance>, JitError> {
    let kind = program_kind(&configuration.shell);
    let mut instances = Vec::new();
    for configured in &configuration.strategy_instances {
        let source = authored_source(configured)?;
        verify_bundle_hash(configured, &source)?;
        let mut parameters = parameters_value(configured)?;
        if parameters.get("symbols").is_none() {
            parameters["symbols"] = serde_json::json!(configured.symbols);
        }
        let runtime = match kind {
            ProgramKind::Study => {
                study_instance(workspace, &source, &parameters, &configured.instance_id)?
            }
            ProgramKind::Strategy => {
                strategy_instance(workspace, &source, &parameters, &configured.instance_id)?
            }
            ProgramKind::Screening => {
                return Err(JitError::Invalid(format!(
                    "instance {:?} is a screening program; screening runs through the one-shot \
                     screen seam, not a backtest configuration",
                    configured.instance_id
                )));
            }
        };
        instances.push(runtime);
    }
    Ok(instances)
}

/// The program kind a configuration shell runs.
#[must_use]
pub fn program_kind(shell: &str) -> ProgramKind {
    if shell == "study" {
        ProgramKind::Study
    } else {
        ProgramKind::Strategy
    }
}

fn authored_source(configured: &StrategyInstanceConfig) -> Result<String, JitError> {
    if configured.implementation != JIT_IMPLEMENTATION {
        return Err(JitError::Invalid(format!(
            "instance {:?} uses implementation {:?}; the embedded Python authoring \
             surface was removed and only implementation \"jit\" programs run",
            configured.instance_id, configured.implementation
        )));
    }
    match configured.parameters.get(SOURCE_PARAMETER) {
        Some(JsonValue::String(text)) if !text.trim().is_empty() => {
            // Git checkouts may carry CRLF; normalize so the bundle-hash
            // provenance digest is checkout-independent.
            Ok(text.replace("\r\n", "\n"))
        }
        Some(_) => Err(JitError::Invalid(format!(
            "instance {:?} parameters.{SOURCE_PARAMETER} must be a non-empty string",
            configured.instance_id
        ))),
        None => Err(JitError::Invalid(format!(
            "instance {:?} is missing parameters.{SOURCE_PARAMETER}: authored Rust \
             source is required",
            configured.instance_id
        ))),
    }
}

fn verify_bundle_hash(configured: &StrategyInstanceConfig, source: &str) -> Result<(), JitError> {
    if configured.bundle_hash.is_empty() {
        return Ok(());
    }
    let digest = harness_contracts::sha256::digest_hex(source.as_bytes());
    if configured.bundle_hash == digest {
        return Ok(());
    }
    Err(JitError::Invalid(format!(
        "instance {:?} bundle_hash {:?} does not match the authored source digest {digest}",
        configured.instance_id, configured.bundle_hash
    )))
}

fn parameters_value(configured: &StrategyInstanceConfig) -> Result<serde_json::Value, JitError> {
    Ok(serde_json::Value::Object(
        configured
            .parameters
            .iter()
            .map(|(key, value)| Ok((key.clone(), json_to_serde(value)?)))
            .collect::<Result<_, JitError>>()?,
    ))
}

fn json_to_serde(value: &JsonValue) -> Result<serde_json::Value, JitError> {
    Ok(match value {
        JsonValue::Null => serde_json::Value::Null,
        JsonValue::Bool(inner) => serde_json::Value::Bool(*inner),
        JsonValue::String(text) => serde_json::Value::String(text.clone()),
        JsonValue::Int(inner) => serde_json::Value::from(*inner),
        JsonValue::UInt(inner) => serde_json::Value::from(*inner),
        JsonValue::Array(items) => {
            serde_json::Value::Array(items.iter().map(json_to_serde).collect::<Result<_, _>>()?)
        }
        JsonValue::Object(entries) if entries.contains_key("$decimal") => {
            if entries.len() != 1 {
                return Err(JitError::Invalid(
                    "parameter decimal wrapper must contain only $decimal".into(),
                ));
            }
            let JsonValue::String(text) = &entries["$decimal"] else {
                return Err(JitError::Invalid(
                    "parameter $decimal must contain decimal text".into(),
                ));
            };
            let number: serde_json::Number = text.parse().map_err(|error| {
                JitError::Invalid(format!("invalid numeric parameter {text:?}: {error}"))
            })?;
            serde_json::Value::Number(number)
        }
        JsonValue::Object(entries) => serde_json::Value::Object(
            entries
                .iter()
                .map(|(key, item)| Ok((key.clone(), json_to_serde(item)?)))
                .collect::<Result<_, JitError>>()?,
        ),
    })
}
