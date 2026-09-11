//! Typed backtest configuration and its canonical-hash representation.
//!
//! Port of the resolved fields of `harness.configuration.models.BacktestConfig`
//! plus `harness.configuration.hashing`. The canonical JSON mirrors pydantic's
//! `model_dump(mode="python")` followed by `_canonical_value`, so
//! [`BacktestConfig::configuration_hash`] reproduces the Python
//! `configuration_hash` byte for byte.

use std::collections::BTreeMap;

use harness_num::PyDecimal;

use crate::backtest::errors::BacktestError;
use crate::backtest::yaml::{self, YamlValue};
use crate::canonical_json::JsonValue;

use super::manifest::{configuration_hash, utc_timestamp};

/// The fee basis used to derive a fee schedule from configuration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FeeBasis {
    /// A flat per-fill commission or minimum.
    Flat,
    /// A per-share or per-contract rate.
    PerUnit,
    /// A notional basis-point rate.
    NotionalBps,
}

impl FeeBasis {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Flat => "flat",
            Self::PerUnit => "per_unit",
            Self::NotionalBps => "notional_bps",
        }
    }

    /// Parse a fee basis from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "flat" => Some(Self::Flat),
            "per_unit" => Some(Self::PerUnit),
            "notional_bps" => Some(Self::NotionalBps),
            _ => None,
        }
    }
}

/// One configured fee rule.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeRule {
    /// Fee identifier.
    pub fee_id: String,
    /// Whether the rule is flat, per-unit, or notional-bps.
    pub basis: FeeBasis,
    /// The string-exact rate.
    pub rate: PyDecimal,
    /// The string-exact minimum.
    pub minimum: PyDecimal,
    /// The order side the rule applies to (`buy`, `sell`, or `both`).
    pub side: String,
}

/// Configured fee policy for one run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeConfig {
    /// ISO-4217 currency code.
    pub currency: String,
    /// Rounding mode name.
    pub rounding: String,
    /// Configured rules.
    pub rules: Vec<FeeRule>,
}

impl Default for FeeConfig {
    fn default() -> Self {
        Self {
            currency: "USD".to_string(),
            rounding: "half_even".to_string(),
            rules: Vec::new(),
        }
    }
}

/// Reproducibility identity for one configured strategy instance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyInstanceConfig {
    /// Instance identifier.
    pub instance_id: String,
    /// `module:Class` implementation path.
    pub implementation: String,
    /// Content hash of the strategy bundle.
    pub bundle_hash: String,
    /// Resolved parameters.
    pub parameters: BTreeMap<String, JsonValue>,
    /// Symbols the instance trades.
    pub symbols: Vec<String>,
    /// String-exact capital fraction.
    pub capital_fraction: String,
}

/// One configured risk rule.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskRuleConfig {
    /// Rule identifier.
    pub rule_id: String,
    /// `module:Class` implementation path.
    pub implementation: String,
    /// Resolved parameters.
    pub parameters: BTreeMap<String, JsonValue>,
}

/// Configured ordered risk pipeline.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskPipelineConfig {
    /// Rejection policy.
    pub on_reject: String,
    /// Ordered rules.
    pub rules: Vec<RiskRuleConfig>,
}

impl Default for RiskPipelineConfig {
    fn default() -> Self {
        Self {
            on_reject: "reject_order".to_string(),
            rules: Vec::new(),
        }
    }
}

/// Resource limits for a deployment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResourceLimitsConfig {
    /// Fixed-point CPU cores.
    pub cpu_cores: PyDecimal,
    /// Memory in mebibytes.
    pub memory_mib: i64,
}

/// Deployment settings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeploymentConfig {
    /// Deployment name.
    pub name: String,
    /// Deployment target.
    pub target: String,
    /// Environment.
    pub environment: String,
    /// Immutable image, if any.
    pub image: Option<String>,
    /// Replica count.
    pub replicas: i64,
    /// Restart policy.
    pub restart_policy: String,
    /// Network access policy.
    pub network_access: String,
    /// Resource limits.
    pub resources: ResourceLimitsConfig,
}

impl Default for DeploymentConfig {
    fn default() -> Self {
        Self {
            name: "backtest-job-local".to_string(),
            target: "local".to_string(),
            environment: "local".to_string(),
            image: None,
            replicas: 1,
            restart_policy: "never".to_string(),
            network_access: "disabled".to_string(),
            resources: ResourceLimitsConfig {
                cpu_cores: PyDecimal::from_i128(1),
                memory_mib: 1024,
            },
        }
    }
}

/// The resolved fields the backtest kernel reads from configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestConfig {
    /// Run identifier.
    pub run_id: String,
    /// Configuration shell (`backtest`).
    pub shell: String,
    /// Schema version.
    pub schema_version: u64,
    /// Clock mode.
    pub clock: String,
    /// Event ordering mode.
    pub event_ordering: String,
    /// Deterministic seed for synthetic data and identifiers.
    pub seed: u64,
    /// Run window start in epoch nanoseconds.
    pub start_ns: i64,
    /// Run window end in epoch nanoseconds.
    pub end_ns: i64,
    /// Base accounting currency.
    pub base_currency: String,
    /// String-exact initial cash.
    pub initial_cash: String,
    /// Initial cash as an exact decimal.
    pub initial_cash_decimal: PyDecimal,
    /// Data snapshot identifier.
    pub data_snapshot_id: String,
    /// Data fingerprint.
    pub data_fingerprint: String,
    /// Data bar interval.
    pub data_bar_interval: String,
    /// Configured symbols.
    pub symbols: Vec<String>,
    /// Fee policy.
    pub fees: FeeConfig,
    /// Risk pipeline.
    pub risk_pipeline: RiskPipelineConfig,
    /// Deployment settings.
    pub deployment: DeploymentConfig,
    /// Configured strategy instances.
    pub strategy_instances: Vec<StrategyInstanceConfig>,
}

impl BacktestConfig {
    /// Build a validated configuration with defaults.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] when the window is empty.
    pub fn new(
        run_id: impl Into<String>,
        start_ns: i64,
        end_ns: i64,
        seed: u64,
    ) -> Result<Self, BacktestError> {
        let run_id = run_id.into();
        if run_id.trim().is_empty() {
            return Err(BacktestError::Configuration(
                "run_id must be a non-empty string".to_string(),
            ));
        }
        if end_ns <= start_ns {
            return Err(BacktestError::Configuration(
                "end must be after start".to_string(),
            ));
        }
        Ok(Self {
            run_id,
            shell: "backtest".to_string(),
            schema_version: 1,
            clock: "event_time".to_string(),
            event_ordering: "timestamp_then_sequence_then_class_then_feed".to_string(),
            seed,
            start_ns,
            end_ns,
            base_currency: "USD".to_string(),
            initial_cash: "1000000".to_string(),
            initial_cash_decimal: PyDecimal::from_i128(1_000_000),
            data_snapshot_id: "synthetic".to_string(),
            data_fingerprint: String::new(),
            data_bar_interval: "1min".to_string(),
            symbols: vec!["SPY".to_string()],
            fees: FeeConfig::default(),
            risk_pipeline: RiskPipelineConfig::default(),
            deployment: DeploymentConfig::default(),
            strategy_instances: Vec::new(),
        })
    }

    /// Whether the configuration drives a backtest shell.
    #[must_use]
    pub fn is_backtest(&self) -> bool {
        self.shell == "backtest"
    }

    /// The domain-separated canonical configuration hash.
    #[must_use]
    pub fn configuration_hash(&self) -> String {
        configuration_hash(&self.canonical_json())
    }

    /// Return the canonical, JSON-ready configuration value.
    #[must_use]
    pub fn canonical_json(&self) -> JsonValue {
        JsonValue::object([
            ("schema_version", JsonValue::UInt(self.schema_version)),
            ("shell", JsonValue::String(self.shell.clone())),
            ("run_id", JsonValue::String(self.run_id.clone())),
            ("start", JsonValue::object([("$datetime", JsonValue::string(utc_timestamp(self.start_ns, true)))])),
            ("end", JsonValue::object([("$datetime", JsonValue::string(utc_timestamp(self.end_ns, true)))])),
            ("clock", JsonValue::String(self.clock.clone())),
            (
                "event_ordering",
                JsonValue::String(self.event_ordering.clone()),
            ),
            ("seed", JsonValue::UInt(self.seed)),
            (
                "base_currency",
                JsonValue::String(self.base_currency.clone()),
            ),
            ("initial_cash", decimal_json(self.initial_cash_decimal)),
            (
                "data",
                JsonValue::object([
                    (
                        "snapshot_id",
                        JsonValue::String(self.data_snapshot_id.clone()),
                    ),
                    (
                        "fingerprint",
                        JsonValue::String(self.data_fingerprint.clone()),
                    ),
                    (
                        "symbols",
                        JsonValue::Array(
                            self.symbols
                                .iter()
                                .map(|symbol| JsonValue::String(symbol.clone()))
                                .collect(),
                        ),
                    ),
                    (
                        "bar_interval",
                        JsonValue::String(self.data_bar_interval.clone()),
                    ),
                ]),
            ),
            (
                "strategy_instances",
                JsonValue::Array(
                    self.strategy_instances
                        .iter()
                        .map(strategy_instance_json)
                        .collect(),
                ),
            ),
            (
                "risk_pipeline",
                JsonValue::object([
                    (
                        "rules",
                        JsonValue::Array(
                            self.risk_pipeline
                                .rules
                                .iter()
                                .map(|rule| {
                                    JsonValue::object([
                                        (
                                            "rule_id",
                                            JsonValue::String(rule.rule_id.clone()),
                                        ),
                                        (
                                            "implementation",
                                            JsonValue::String(rule.implementation.clone()),
                                        ),
                                        (
                                            "parameters",
                                            JsonValue::Object(rule.parameters.clone()),
                                        ),
                                    ])
                                })
                                .collect(),
                        ),
                    ),
                    (
                        "on_reject",
                        JsonValue::String(self.risk_pipeline.on_reject.clone()),
                    ),
                ]),
            ),
            (
                "fees",
                JsonValue::object([
                    ("currency", JsonValue::String(self.fees.currency.clone())),
                    ("rounding", JsonValue::String(self.fees.rounding.clone())),
                    (
                        "rules",
                        JsonValue::Array(
                            self.fees
                                .rules
                                .iter()
                                .map(|rule| {
                                    JsonValue::object([
                                        (
                                            "fee_id",
                                            JsonValue::String(rule.fee_id.clone()),
                                        ),
                                        ("basis", JsonValue::string(rule.basis.value())),
                                        ("rate", decimal_json(rule.rate)),
                                        ("minimum", decimal_json(rule.minimum)),
                                        ("side", JsonValue::String(rule.side.clone())),
                                    ])
                                })
                                .collect(),
                        ),
                    ),
                ]),
            ),
            ("deployment", deployment_json(&self.deployment)),
        ])
    }

    /// Parse a validated backtest configuration from a YAML document.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] on any malformed document.
    pub fn from_yaml(text: &str) -> Result<Self, BacktestError> {
        let value = yaml::load_yaml(text)
            .map_err(|error| BacktestError::Configuration(error.0))?;
        Self::from_yaml_value(&value)
    }

    /// Parse a validated backtest configuration from a parsed YAML mapping.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] on any malformed document.
    pub fn from_yaml_value(value: &YamlValue) -> Result<Self, BacktestError> {
        let root = value
            .as_mapping()
            .ok_or_else(|| BacktestError::Configuration("configuration root must be a mapping".to_string()))?;
        let shell = string_field(root, "shell")?;
        if shell != "backtest" {
            return Err(BacktestError::Configuration(format!(
                "configuration shell must be 'backtest', not '{shell}'"
            )));
        }
        let run_id = string_field(root, "run_id")?;
        let start_ns = datetime_field(root, "start")
            .unwrap_or(crate::backtest::job::DEFAULT_START_NS);
        let end_ns = datetime_field(root, "end")
            .unwrap_or(crate::backtest::job::DEFAULT_END_NS);
        let seed = int_field(root, "seed").unwrap_or(0);
        if seed < 0 {
            return Err(BacktestError::Configuration(
                "seed must be non-negative".to_string(),
            ));
        }
        let mut config = Self::new(run_id, start_ns, end_ns, seed as u64)?;
        config.shell = shell;
        config.schema_version = int_field(root, "schema_version").unwrap_or(1) as u64;
        if let Some(clock) = root.get("clock").and_then(YamlValue::as_str) {
            config.clock = clock.to_string();
        }
        if let Some(ordering) = root.get("event_ordering").and_then(YamlValue::as_str) {
            config.event_ordering = ordering.to_string();
        }
        if let Some(currency) = root.get("base_currency").and_then(YamlValue::as_str) {
            config.base_currency = currency.to_string();
        }
        if let Some(cash) = root.get("initial_cash").and_then(YamlValue::as_decimal) {
            config.initial_cash_decimal = cash;
            config.initial_cash = py_decimal_fixed(cash);
        }
        if let Some(data) = root.get("data").and_then(YamlValue::as_mapping) {
            if let Some(snapshot) = data.get("snapshot_id").and_then(YamlValue::as_str) {
                config.data_snapshot_id = snapshot.to_string();
            }
            if let Some(fingerprint) = data.get("fingerprint").and_then(YamlValue::as_str) {
                config.data_fingerprint = fingerprint.to_string();
            }
            if let Some(interval) = data.get("bar_interval").and_then(YamlValue::as_str) {
                config.data_bar_interval = interval.to_string();
            }
            if let Some(symbols) = data.get("symbols").and_then(YamlValue::as_sequence) {
                config.symbols = canonical_symbols(symbols)?;
            }
        }
        config.fees = parse_fees(root.get("fees"))?;
        config.risk_pipeline = parse_risk(root.get("risk_pipeline"))?;
        config.deployment = parse_deployment(root.get("deployment"))?;
        config.strategy_instances = parse_strategy_instances(root.get("strategy_instances"))?;
        Ok(config)
    }
}

fn strategy_instance_json(instance: &StrategyInstanceConfig) -> JsonValue {
    JsonValue::object([
        (
            "instance_id",
            JsonValue::String(instance.instance_id.clone()),
        ),
        (
            "implementation",
            JsonValue::String(instance.implementation.clone()),
        ),
        (
            "bundle_hash",
            JsonValue::String(instance.bundle_hash.clone()),
        ),
        (
            "capital_fraction",
            decimal_json(
                instance
                    .capital_fraction
                    .parse::<PyDecimal>()
                    .unwrap_or(PyDecimal::ZERO),
            ),
        ),
        (
            "symbols",
            JsonValue::Array(
                instance
                    .symbols
                    .iter()
                    .map(|symbol| JsonValue::String(symbol.clone()))
                    .collect(),
            ),
        ),
        (
            "parameters",
            JsonValue::Object(instance.parameters.clone()),
        ),
    ])
}

fn deployment_json(deployment: &DeploymentConfig) -> JsonValue {
    JsonValue::object([
        ("name", JsonValue::String(deployment.name.clone())),
        ("target", JsonValue::String(deployment.target.clone())),
        (
            "environment",
            JsonValue::String(deployment.environment.clone()),
        ),
        (
            "image",
            match &deployment.image {
                Some(image) => JsonValue::String(image.clone()),
                None => JsonValue::Null,
            },
        ),
        ("replicas", JsonValue::Int(deployment.replicas)),
        (
            "restart_policy",
            JsonValue::String(deployment.restart_policy.clone()),
        ),
        (
            "network_access",
            JsonValue::String(deployment.network_access.clone()),
        ),
        (
            "resources",
            JsonValue::object([
                ("cpu_cores", decimal_json(deployment.resources.cpu_cores)),
                ("memory_mib", JsonValue::Int(deployment.resources.memory_mib)),
            ]),
        ),
    ])
}

/// One `{"$decimal": <fixed>}` wrapper, matching `_decimal_text`.
fn decimal_json(value: PyDecimal) -> JsonValue {
    JsonValue::object([("$decimal", JsonValue::string(normalized_fixed(value)))])
}

/// Python `format(value.normalize(), "f")`.
#[must_use]
pub fn normalized_fixed(value: PyDecimal) -> String {
    if value.is_zero() {
        return "0".to_string();
    }
    let normalized = value.normalize().unwrap_or(value);
    py_decimal_fixed(normalized)
}

/// Render an exact decimal in fixed-point notation, expanding any exponent.
#[must_use]
pub fn py_decimal_fixed(value: PyDecimal) -> String {
    let negative = value.is_signed() && !value.is_zero();
    let coefficient = value.coefficient();
    let scale = value.scale();
    let mut out = String::new();
    if negative {
        out.push('-');
    }
    let digits = coefficient.to_string();
    if scale <= 0 {
        out.push_str(&digits);
        for _ in 0..(-scale) {
            out.push('0');
        }
    } else {
        let scale = scale as usize;
        if digits.len() > scale {
            let point = digits.len() - scale;
            out.push_str(&digits[..point]);
            out.push('.');
            out.push_str(&digits[point..]);
        } else {
            out.push_str("0.");
            for _ in 0..(scale - digits.len()) {
                out.push('0');
            }
            out.push_str(&digits);
        }
    }
    out
}

fn canonical_symbols(symbols: &[YamlValue]) -> Result<Vec<String>, BacktestError> {
    let mut canonical: Vec<String> = symbols
        .iter()
        .map(|symbol| {
            symbol
                .as_str()
                .map(str::to_uppercase)
                .ok_or_else(|| BacktestError::Configuration("symbols must be strings".to_string()))
        })
        .collect::<Result<_, _>>()?;
    canonical.sort();
    let unique: std::collections::BTreeSet<&String> = canonical.iter().collect();
    if unique.len() != canonical.len() || canonical.is_empty() {
        return Err(BacktestError::Configuration(
            "symbols must be non-empty and unique".to_string(),
        ));
    }
    Ok(canonical)
}

fn parse_fees(value: Option<&YamlValue>) -> Result<FeeConfig, BacktestError> {
    let Some(mapping) = value.and_then(YamlValue::as_mapping) else {
        return Ok(FeeConfig::default());
    };
    let currency = mapping
        .get("currency")
        .and_then(YamlValue::as_str)
        .unwrap_or("USD")
        .to_string();
    let rounding = mapping
        .get("rounding")
        .and_then(YamlValue::as_str)
        .unwrap_or("half_even")
        .to_string();
    let mut rules = Vec::new();
    if let Some(items) = mapping.get("rules").and_then(YamlValue::as_sequence) {
        for item in items {
            let rule = item.as_mapping().ok_or_else(|| {
                BacktestError::Configuration("fee rules must be mappings".to_string())
            })?;
            let basis = string_field(rule, "basis")?;
            rules.push(FeeRule {
                fee_id: string_field(rule, "fee_id")?,
                basis: FeeBasis::parse(&basis).ok_or_else(|| {
                    BacktestError::Configuration(format!("unknown fee basis {basis:?}"))
                })?,
                rate: decimal_field(rule, "rate")?,
                minimum: rule
                    .get("minimum")
                    .and_then(YamlValue::as_decimal)
                    .unwrap_or(PyDecimal::ZERO),
                side: rule
                    .get("side")
                    .and_then(YamlValue::as_str)
                    .unwrap_or("both")
                    .to_string(),
            });
        }
    }
    Ok(FeeConfig {
        currency,
        rounding,
        rules,
    })
}

fn parse_risk(value: Option<&YamlValue>) -> Result<RiskPipelineConfig, BacktestError> {
    let Some(mapping) = value.and_then(YamlValue::as_mapping) else {
        return Ok(RiskPipelineConfig::default());
    };
    let on_reject = mapping
        .get("on_reject")
        .and_then(YamlValue::as_str)
        .unwrap_or("reject_order")
        .to_string();
    let mut rules = Vec::new();
    if let Some(items) = mapping.get("rules").and_then(YamlValue::as_sequence) {
        for item in items {
            let rule = item.as_mapping().ok_or_else(|| {
                BacktestError::Configuration("risk rules must be mappings".to_string())
            })?;
            rules.push(RiskRuleConfig {
                rule_id: string_field(rule, "rule_id")?,
                implementation: string_field(rule, "implementation")?,
                parameters: parameters(rule.get("parameters"))?,
            });
        }
    }
    Ok(RiskPipelineConfig { on_reject, rules })
}

fn parse_strategy_instances(
    value: Option<&YamlValue>,
) -> Result<Vec<StrategyInstanceConfig>, BacktestError> {
    let Some(items) = value.and_then(YamlValue::as_sequence) else {
        return Ok(Vec::new());
    };
    let mut instances = Vec::new();
    for item in items {
        let instance = item.as_mapping().ok_or_else(|| {
            BacktestError::Configuration("strategy instances must be mappings".to_string())
        })?;
        let capital_fraction = instance
            .get("capital_fraction")
            .and_then(YamlValue::as_decimal)
            .map(py_decimal_fixed)
            .unwrap_or_else(|| "1.0".to_string());
        instances.push(StrategyInstanceConfig {
            instance_id: string_field(instance, "instance_id")?,
            implementation: string_field(instance, "implementation")?,
            bundle_hash: string_field(instance, "bundle_hash")?,
            parameters: parameters(instance.get("parameters"))?,
            symbols: instance
                .get("symbols")
                .and_then(YamlValue::as_sequence)
                .map(canonical_symbols)
                .transpose()?
                .unwrap_or_default(),
            capital_fraction,
        });
    }
    Ok(instances)
}

fn parse_deployment(value: Option<&YamlValue>) -> Result<DeploymentConfig, BacktestError> {
    let Some(mapping) = value.and_then(YamlValue::as_mapping) else {
        return Ok(DeploymentConfig::default());
    };
    let defaults = DeploymentConfig::default();
    let resources = mapping.get("resources").and_then(YamlValue::as_mapping);
    Ok(DeploymentConfig {
        name: mapping
            .get("name")
            .and_then(YamlValue::as_str)
            .unwrap_or(&defaults.name)
            .to_string(),
        target: mapping
            .get("target")
            .and_then(YamlValue::as_str)
            .unwrap_or(&defaults.target)
            .to_string(),
        environment: mapping
            .get("environment")
            .and_then(YamlValue::as_str)
            .unwrap_or(&defaults.environment)
            .to_string(),
        image: mapping
            .get("image")
            .and_then(YamlValue::as_str)
            .map(str::to_string),
        replicas: mapping
            .get("replicas")
            .and_then(YamlValue::as_int)
            .unwrap_or(defaults.replicas),
        restart_policy: mapping
            .get("restart_policy")
            .and_then(YamlValue::as_str)
            .unwrap_or(&defaults.restart_policy)
            .to_string(),
        network_access: mapping
            .get("network_access")
            .and_then(YamlValue::as_str)
            .unwrap_or(&defaults.network_access)
            .to_string(),
        resources: ResourceLimitsConfig {
            cpu_cores: resources
                .and_then(|entry| entry.get("cpu_cores"))
                .and_then(YamlValue::as_decimal)
                .unwrap_or(defaults.resources.cpu_cores),
            memory_mib: resources
                .and_then(|entry| entry.get("memory_mib"))
                .and_then(YamlValue::as_int)
                .unwrap_or(defaults.resources.memory_mib),
        },
    })
}

fn parameters(value: Option<&YamlValue>) -> Result<BTreeMap<String, JsonValue>, BacktestError> {
    let Some(mapping) = value.and_then(YamlValue::as_mapping) else {
        return Ok(BTreeMap::new());
    };
    Ok(mapping
        .iter()
        .map(|(key, value)| (key.clone(), yaml_to_json(value)))
        .collect())
}

fn yaml_to_json(value: &YamlValue) -> JsonValue {
    match value {
        YamlValue::Null => JsonValue::Null,
        YamlValue::Bool(value) => JsonValue::Bool(*value),
        YamlValue::Int(value) => JsonValue::Int(*value),
        YamlValue::Decimal(value) => decimal_json(*value),
        YamlValue::String(value) => JsonValue::String(value.clone()),
        YamlValue::Sequence(items) => {
            JsonValue::Array(items.iter().map(yaml_to_json).collect())
        }
        YamlValue::Mapping(entries) => JsonValue::Object(
            entries
                .iter()
                .map(|(key, value)| (key.clone(), yaml_to_json(value)))
                .collect(),
        ),
    }
}

fn string_field(mapping: &BTreeMap<String, YamlValue>, key: &str) -> Result<String, BacktestError> {
    mapping
        .get(key)
        .and_then(YamlValue::as_str)
        .map(str::to_string)
        .ok_or_else(|| BacktestError::Configuration(format!("missing string field {key:?}")))
}

fn int_field(mapping: &BTreeMap<String, YamlValue>, key: &str) -> Option<i64> {
    mapping.get(key).and_then(YamlValue::as_int)
}

fn decimal_field(
    mapping: &BTreeMap<String, YamlValue>,
    key: &str,
) -> Result<PyDecimal, BacktestError> {
    mapping
        .get(key)
        .and_then(YamlValue::as_decimal)
        .ok_or_else(|| BacktestError::Configuration(format!("missing decimal field {key:?}")))
}

fn datetime_field(
    mapping: &BTreeMap<String, YamlValue>,
    key: &str,
) -> Option<i64> {
    let text = mapping.get(key).and_then(YamlValue::as_str)?;
    crate::backtest::job::parse_timestamp_ns(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    const CANONICAL_HASH: &str =
        "9076d70fa89c3624ce292824665f45f5c6e5afa74837f92084ee7d1bd629adf5";

    fn repo_config_path() -> std::path::PathBuf {
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("..")
            .join("config")
            .join("harness")
            .join("backtest.yaml")
    }

    #[test]
    fn canonical_configuration_hash_matches_python() {
        let text = std::fs::read_to_string(repo_config_path()).expect("read config");
        let config = BacktestConfig::from_yaml(&text).expect("parse");
        assert_eq!(config.configuration_hash(), CANONICAL_HASH);
    }

    #[test]
    fn normalized_decimal_matches_python_format() {
        assert_eq!(
            normalized_fixed("100000.00".parse().unwrap()),
            "100000"
        );
        assert_eq!(normalized_fixed("1.0".parse().unwrap()), "1");
        assert_eq!(normalized_fixed("0.005".parse().unwrap()), "0.005");
        assert_eq!(normalized_fixed("0.01".parse().unwrap()), "0.01");
        assert_eq!(normalized_fixed("10000.00".parse().unwrap()), "10000");
    }

    #[test]
    fn rejects_non_backtest_shell() {
        assert!(BacktestConfig::from_yaml("shell: api\n").is_err());
    }
}
