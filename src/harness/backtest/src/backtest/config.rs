//! Typed backtest configuration and its canonical-hash representation.
//!
//! Projects the shared `config` document schema into engine decimal and time
//! types. Resolved engine identities retain the shipped ASCII-escaped v1 hash;
//! submitted API document identities deliberately remain separate.

use std::collections::BTreeMap;

use num::Decimal;

use crate::backtest::subscriptions::normalize_subscriptions;
use crate::backtest::yaml;
use crate::backtest::yaml::YamlValue;
use harness_contracts::canonical_json::JsonValue;
use harness_trading::program::MarketSubscription;
use harness_trading::replay_errors::BacktestError;

use super::manifest::configuration_hash;
use super::manifest::configuration_timestamp;

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
    pub rate: Decimal,
    /// The string-exact minimum.
    pub minimum: Decimal,
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

/// Optional signal-destruction null transform for a replay run.
///
/// Only emitted into the canonical configuration when it was supplied, so
/// absent-null documents keep their shipped identities and hashes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NullTransformConfig {
    /// `permutation` shuffles individual returns; `block_permutation` shuffles blocks.
    pub mode: String,
    /// Consecutive log-return block size, `1..=10000`; ordinary permutation requires 1.
    pub block_size: u32,
    /// Permutation seed, non-negative.
    pub seed: u64,
}

impl NullTransformConfig {
    /// Validate supported method semantics, including programmatically built requests.
    pub fn validate(&self) -> Result<(), BacktestError> {
        if self.mode != "block_permutation" && self.mode != "permutation" {
            return Err(BacktestError::Configuration(format!(
                "null.mode must be permutation or block_permutation, got {:?}",
                self.mode
            )));
        }
        if !(1..=10_000).contains(&self.block_size) {
            return Err(BacktestError::Configuration(
                "null.block_size must be between 1 and 10000".into(),
            ));
        }
        if self.mode == "permutation" && self.block_size != 1 {
            return Err(BacktestError::Configuration(
                "permutation requires block_size=1".into(),
            ));
        }
        Ok(())
    }
}

/// Resource limits for a deployment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResourceLimitsConfig {
    /// Fixed-point CPU cores.
    pub cpu_cores: Decimal,
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
                cpu_cores: Decimal::from_i128(1),
                memory_mib: 1024,
            },
        }
    }
}

/// The resolved fields the backtest kernel reads from configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestConfig {
    /// Job identifier.
    pub job_id: String,
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
    pub initial_cash_decimal: Decimal,
    /// Data snapshot identifier.
    pub data_snapshot_id: String,
    /// Data fingerprint.
    pub data_fingerprint: String,
    /// Data bar interval.
    pub data_bar_interval: String,
    /// Source fixture cadence, distinct from requested output intervals.
    pub data_source_bar_interval: String,
    /// Optional expected echo of the runtime requirement union.
    pub data_subscriptions: Vec<MarketSubscription>,
    /// Distinguishes an absent echo from an explicitly empty echo.
    pub data_subscriptions_declared: bool,
    /// Stable input names mapped to point-in-time individual option selectors.
    pub option_selectors: BTreeMap<String, harness_contracts::OptionSelector>,
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
    /// Optional signal-destruction null transform.
    pub null_transform: Option<NullTransformConfig>,
}

impl BacktestConfig {
    /// Build a validated configuration with defaults.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] when the window is empty.
    pub fn new(
        job_id: impl Into<String>,
        start_ns: i64,
        end_ns: i64,
        seed: u64,
    ) -> Result<Self, BacktestError> {
        let job_id = job_id.into();
        if job_id.trim().is_empty() {
            return Err(BacktestError::Configuration(
                "job_id must be a non-empty string".to_string(),
            ));
        }
        if end_ns <= start_ns {
            return Err(BacktestError::Configuration(
                "end must be after start".to_string(),
            ));
        }
        Ok(Self {
            job_id,
            shell: "backtest".to_string(),
            schema_version: 1,
            clock: "event_time".to_string(),
            event_ordering: "timestamp_then_sequence_then_class_then_feed".to_string(),
            seed,
            start_ns,
            end_ns,
            base_currency: "USD".to_string(),
            initial_cash: "1000000".to_string(),
            initial_cash_decimal: Decimal::from_i128(1_000_000),
            data_snapshot_id: "synthetic".to_string(),
            data_fingerprint: String::new(),
            data_bar_interval: "1min".to_string(),
            data_source_bar_interval: "1min".to_string(),
            data_subscriptions: Vec::new(),
            data_subscriptions_declared: false,
            option_selectors: BTreeMap::new(),
            symbols: vec!["SPY".to_string()],
            fees: FeeConfig::default(),
            risk_pipeline: RiskPipelineConfig::default(),
            deployment: DeploymentConfig::default(),
            strategy_instances: Vec::new(),
            null_transform: None,
        })
    }

    /// Whether the configuration drives a backtest shell.
    #[must_use]
    pub fn is_backtest(&self) -> bool {
        self.shell == "backtest"
    }

    /// Whether the configuration drives a replay shell (backtest or study).
    ///
    /// A study reuses the deterministic replay substrate with no order
    /// execution, so the data preflight accepts it exactly like a backtest.
    #[must_use]
    pub fn is_replay(&self) -> bool {
        self.shell == "backtest" || self.shell == "study"
    }

    /// Whether an expected subscription echo was provided.
    #[must_use]
    pub fn has_subscription_echo(&self) -> bool {
        self.data_subscriptions_declared || !self.data_subscriptions.is_empty()
    }

    /// The domain-separated canonical configuration hash.
    #[must_use]
    pub fn configuration_hash(&self) -> String {
        configuration_hash(&self.canonical_json())
    }

    /// Return the canonical, JSON-ready configuration value.
    #[must_use]
    pub fn canonical_json(&self) -> JsonValue {
        let mut value = JsonValue::object([
            ("schema_version", JsonValue::UInt(self.schema_version)),
            ("shell", JsonValue::String(self.shell.clone())),
            ("job_id", JsonValue::String(self.job_id.clone())),
            (
                "start",
                JsonValue::object([(
                    "$datetime",
                    JsonValue::string(configuration_timestamp(self.start_ns)),
                )]),
            ),
            (
                "end",
                JsonValue::object([(
                    "$datetime",
                    JsonValue::string(configuration_timestamp(self.end_ns)),
                )]),
            ),
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
            ("data", self.data_json()),
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
                                        ("rule_id", JsonValue::String(rule.rule_id.clone())),
                                        (
                                            "implementation",
                                            JsonValue::String(rule.implementation.clone()),
                                        ),
                                        ("parameters", JsonValue::Object(rule.parameters.clone())),
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
                                        ("fee_id", JsonValue::String(rule.fee_id.clone())),
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
        ]);
        if let Some(null_transform) = &self.null_transform {
            if let JsonValue::Object(entries) = &mut value {
                entries.insert(
                    "null".to_string(),
                    JsonValue::object([
                        ("mode", JsonValue::String(null_transform.mode.clone())),
                        (
                            "block_size",
                            JsonValue::UInt(u64::from(null_transform.block_size)),
                        ),
                        ("seed", JsonValue::UInt(null_transform.seed)),
                    ]),
                );
            }
        }
        value
    }

    fn data_json(&self) -> JsonValue {
        let mut data = BTreeMap::from([
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
        ]);
        if self
            .data_subscriptions
            .iter()
            .any(|input| input.data_type == "bars")
        {
            data.insert(
                "bar_interval",
                JsonValue::String(self.data_bar_interval.clone()),
            );
        }
        if self.has_subscription_echo() {
            data.insert(
                "subscriptions",
                subscriptions_json(&self.data_subscriptions),
            );
        }
        if !self.option_selectors.is_empty() {
            data.insert(
                "options",
                yaml_to_json(
                    &canonical_to_engine_yaml(
                        &serde_json::to_value(&self.option_selectors)
                            .expect("option selector JSON"),
                    )
                    .expect("option selector values"),
                ),
            );
        }
        JsonValue::object(data)
    }

    /// Parse a validated backtest configuration from a YAML document.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] on any malformed document.
    pub fn from_yaml(text: &str) -> Result<Self, BacktestError> {
        let value = yaml::load_yaml(text).map_err(|error| BacktestError::Configuration(error.0))?;
        Self::from_yaml_value(&value)
    }

    /// Parse a validated backtest configuration from a parsed YAML mapping.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] on any malformed document.
    pub fn from_yaml_value(value: &YamlValue) -> Result<Self, BacktestError> {
        // The strict document schema is shared with API and MCP; below is only
        // an engine projection of already validated, normalized fields.
        let submitted = to_contract_yaml(value);
        let config::YamlValue::Map(mapping) = submitted else {
            return Err(BacktestError::Configuration(
                "configuration root must be a mapping".into(),
            ));
        };
        let canonical = config::replay::validate_mapping(&mapping)
            .map_err(|e| BacktestError::Configuration(e.0))?;
        let value = canonical_to_engine_yaml(&canonical)?;
        let root = value.as_mapping().ok_or_else(|| {
            BacktestError::Configuration("configuration root must be a mapping".to_string())
        })?;
        let shell = string_field(root, "shell")?;
        if shell != "backtest" && shell != "study" {
            return Err(BacktestError::Configuration(format!(
                "configuration shell must be 'backtest' or 'study', not '{shell}'"
            )));
        }
        let job_id = string_field(root, "job_id")?;
        let start_ns = datetime_field(root, "start")?.unwrap_or(DEFAULT_START_NS);
        let end_ns = datetime_field(root, "end")?.unwrap_or(DEFAULT_END_NS);
        let seed = int_field(root, "seed").unwrap_or(0);
        if seed < 0 {
            return Err(BacktestError::Configuration(
                "seed must be non-negative".to_string(),
            ));
        }
        let mut config = Self::new(job_id, start_ns, end_ns, seed as u64)?;
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
            if let Some(options) = data.get("options") {
                config.option_selectors =
                    serde_json::from_value(yaml_to_json(options).to_serde_value())
                        .map_err(|e| BacktestError::Configuration(format!("data.options: {e}")))?;
                for selector in config.option_selectors.values() {
                    let as_of = selector
                        .validate()
                        .map_err(|e| BacktestError::Configuration(e.to_string()))?;
                    if as_of.to_nanos_i64() > config.start_ns {
                        return Err(BacktestError::Configuration(
                            "option selection as_of must not follow replay start".into(),
                        ));
                    }
                }
            }
            if let Some(value) = data.get("subscriptions") {
                config.data_subscriptions_declared = true;
                let items = value.as_sequence().ok_or_else(|| {
                    BacktestError::Configuration("data.subscriptions must be an array".into())
                })?;
                let mut inputs = Vec::new();
                for item in items {
                    let mapping = item.as_mapping().ok_or_else(|| {
                        BacktestError::Configuration(
                            "data.subscriptions entries must be mappings".into(),
                        )
                    })?;
                    if mapping
                        .keys()
                        .any(|key| !matches!(key.as_str(), "data_type" | "symbols" | "interval"))
                    {
                        return Err(BacktestError::Configuration(
                            "unknown field in data.subscriptions".into(),
                        ));
                    }
                    let symbols = mapping
                        .get("symbols")
                        .and_then(YamlValue::as_sequence)
                        .ok_or_else(|| {
                            BacktestError::Configuration(
                                "subscription symbols must be an array".into(),
                            )
                        })?;
                    let symbols = symbols
                        .iter()
                        .map(|s| {
                            s.as_str().map(str::to_string).ok_or_else(|| {
                                BacktestError::Configuration(
                                    "subscription symbols must be strings".into(),
                                )
                            })
                        })
                        .collect::<Result<Vec<_>, _>>()?;
                    let interval = match mapping.get("interval") {
                        None | Some(YamlValue::Null) => None,
                        Some(YamlValue::String(s)) => Some(s.clone()),
                        _ => {
                            return Err(BacktestError::Configuration(
                                "subscription interval must be a string".into(),
                            ))
                        }
                    };
                    inputs.push(MarketSubscription {
                        data_type: string_field(mapping, "data_type")?,
                        symbols,
                        interval,
                    });
                }
                config.data_subscriptions = normalize_subscriptions(&inputs)?;
            }
            if let Some(snapshot) = data.get("snapshot_id").and_then(YamlValue::as_str) {
                config.data_snapshot_id = snapshot.to_string();
            }
            if let Some(fingerprint) = data.get("fingerprint").and_then(YamlValue::as_str) {
                config.data_fingerprint = fingerprint.to_string();
            }
            if let Some(interval) = data.get("bar_interval").and_then(YamlValue::as_str) {
                config.data_bar_interval = interval.to_string();
                config.data_source_bar_interval = interval.to_string();
            }
            if let Some(symbols) = data.get("symbols").and_then(YamlValue::as_sequence) {
                config.symbols = canonical_symbols(symbols)?;
            }
        }
        config.fees = parse_fees(root.get("fees"))?;
        config.risk_pipeline = parse_risk(root.get("risk_pipeline"))?;
        config.deployment = parse_deployment(root.get("deployment"))?;
        config.strategy_instances = parse_strategy_instances(root.get("strategy_instances"))?;
        config.null_transform = parse_null_transform(root.get("null"))?;
        if config.is_replay() && !config.data_subscriptions_declared {
            return Err(BacktestError::Configuration(
                "replay configurations must declare data.subscriptions".into(),
            ));
        }
        Ok(config)
    }
}

pub(crate) fn to_contract_yaml(value: &YamlValue) -> config::YamlValue {
    match value {
        YamlValue::Null => config::YamlValue::Null,
        YamlValue::Bool(v) => config::YamlValue::Bool(*v),
        YamlValue::Int(v) => config::YamlValue::Int(*v),
        YamlValue::Decimal(v) => config::YamlValue::Decimal(py_decimal_fixed(*v)),
        YamlValue::String(v) => config::YamlValue::Str(v.clone()),
        YamlValue::Sequence(v) => config::YamlValue::Seq(v.iter().map(to_contract_yaml).collect()),
        YamlValue::Mapping(v) => config::YamlValue::Map(
            v.iter()
                .map(|(k, v)| (k.clone(), to_contract_yaml(v)))
                .collect(),
        ),
    }
}

fn canonical_to_engine_yaml(value: &serde_json::Value) -> Result<YamlValue, BacktestError> {
    use serde_json::Value;
    Ok(match value {
        Value::Object(map) if map.len() == 1 && map.contains_key("$decimal") => YamlValue::Decimal(
            map["$decimal"]
                .as_str()
                .unwrap_or_default()
                .parse()
                .map_err(|_| {
                    BacktestError::Configuration("decimal exceeds engine precision".into())
                })?,
        ),
        Value::Object(map) if map.len() == 1 && map.contains_key("$datetime") => {
            YamlValue::String(map["$datetime"].as_str().unwrap_or_default().to_string())
        }
        Value::Object(map) => YamlValue::Mapping(
            map.iter()
                .map(|(k, v)| Ok((k.clone(), canonical_to_engine_yaml(v)?)))
                .collect::<Result<_, BacktestError>>()?,
        ),
        Value::Array(items) => YamlValue::Sequence(
            items
                .iter()
                .map(canonical_to_engine_yaml)
                .collect::<Result<_, _>>()?,
        ),
        Value::String(v) => YamlValue::String(v.clone()),
        Value::Number(v) => {
            YamlValue::Int(v.as_i64().ok_or_else(|| {
                BacktestError::Configuration("integer outside engine range".into())
            })?)
        }
        Value::Bool(v) => YamlValue::Bool(*v),
        Value::Null => YamlValue::Null,
    })
}

pub(crate) fn subscriptions_json(inputs: &[MarketSubscription]) -> JsonValue {
    JsonValue::Array(
        inputs
            .iter()
            .map(|s| {
                let mut entries = vec![
                    ("data_type", JsonValue::String(s.data_type.clone())),
                    (
                        "symbols",
                        JsonValue::Array(
                            s.symbols.iter().cloned().map(JsonValue::String).collect(),
                        ),
                    ),
                ];
                if let Some(interval) = &s.interval {
                    entries.push(("interval", JsonValue::String(interval.clone())));
                }
                JsonValue::object(entries)
            })
            .collect(),
    )
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
                    .parse::<Decimal>()
                    .unwrap_or(Decimal::ZERO),
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
        ("parameters", JsonValue::Object(instance.parameters.clone())),
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
                (
                    "memory_mib",
                    JsonValue::Int(deployment.resources.memory_mib),
                ),
            ]),
        ),
    ])
}

/// One `{"$decimal": <fixed>}` wrapper, matching `_decimal_text`.
fn decimal_json(value: Decimal) -> JsonValue {
    JsonValue::object([("$decimal", JsonValue::string(normalized_fixed(value)))])
}

/// Python `format(value.normalize(), "f")`.
#[must_use]
pub fn normalized_fixed(value: Decimal) -> String {
    if value.is_zero() {
        return "0".to_string();
    }
    let normalized = value.normalize().unwrap_or(value);
    py_decimal_fixed(normalized)
}

/// Render an exact decimal in fixed-point notation, expanding any exponent.
#[must_use]
pub fn py_decimal_fixed(value: Decimal) -> String {
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
                    .unwrap_or(Decimal::ZERO),
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

fn parse_null_transform(
    value: Option<&YamlValue>,
) -> Result<Option<NullTransformConfig>, BacktestError> {
    let Some(mapping) = value.and_then(YamlValue::as_mapping) else {
        return Ok(None);
    };
    let mode = mapping
        .get("mode")
        .and_then(YamlValue::as_str)
        .unwrap_or("block_permutation")
        .to_string();
    let block_size = mapping
        .get("block_size")
        .and_then(YamlValue::as_int)
        .ok_or_else(|| {
            BacktestError::Configuration("null.block_size must be an integer".to_string())
        })?;
    let seed = mapping
        .get("seed")
        .and_then(YamlValue::as_int)
        .ok_or_else(|| BacktestError::Configuration("null.seed must be an integer".to_string()))?;
    if seed < 0 {
        return Err(BacktestError::Configuration(
            "null.seed must not be negative".to_string(),
        ));
    }
    let null = NullTransformConfig {
        mode,
        block_size: u32::try_from(block_size).map_err(|_| {
            BacktestError::Configuration("null.block_size must be between 1 and 10000".into())
        })?,
        seed: seed as u64,
    };
    null.validate()?;
    Ok(Some(null))
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
        YamlValue::Sequence(items) => JsonValue::Array(items.iter().map(yaml_to_json).collect()),
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
) -> Result<Decimal, BacktestError> {
    mapping
        .get(key)
        .and_then(YamlValue::as_decimal)
        .ok_or_else(|| BacktestError::Configuration(format!("missing decimal field {key:?}")))
}

fn datetime_field(
    mapping: &BTreeMap<String, YamlValue>,
    key: &str,
) -> Result<Option<i64>, BacktestError> {
    let Some(value) = mapping.get(key) else {
        return Ok(None);
    };
    let invalid = || {
        BacktestError::Configuration(format!(
            "{key} must be a valid RFC3339 timestamp within nanosecond range"
        ))
    };
    let text = value.as_str().ok_or_else(invalid)?;
    parse_timestamp_ns(text).map(Some).ok_or_else(invalid)
}

/// Parse an RFC3339 timestamp with explicit offset at exact nanosecond precision.
/// Returns `None` for malformed or out-of-range bounds rather than rounding.
#[must_use]
pub fn parse_timestamp_ns(value: &str) -> Option<i64> {
    let text = value.trim();
    if !text.is_ascii() || text.len() < 20 {
        return None;
    }
    let digits = |part: &str| -> Option<i64> {
        if part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        part.parse().ok()
    };
    if !matches!(text.as_bytes()[10], b'T' | b't') || &text[4..5] != "-" || &text[7..8] != "-" {
        return None;
    }
    digits(&text[..4])?;
    digits(&text[5..7])?;
    digits(&text[8..10])?;
    let date = marketdata::Date::parse(&text[..10])?;
    let time = &text[11..];
    let (clock, offset) = if time.ends_with(['Z', 'z']) {
        (&time[..time.len() - 1], 0)
    } else {
        let index = time.rfind(['+', '-'])?;
        let zone = &time[index..];
        if zone.len() != 6 || &zone[3..4] != ":" {
            return None;
        }
        let hours = digits(&zone[1..3])?;
        let minutes = digits(&zone[4..6])?;
        if hours > 23 || minutes > 59 {
            return None;
        }
        let sign = if zone.starts_with('+') { 1 } else { -1 };
        (&time[..index], sign * (hours * 3600 + minutes * 60))
    };
    let (clock, fraction) = match clock.split_once('.') {
        Some((clock, fraction)) => {
            if fraction.len() > 9 {
                return None;
            }
            let nanos = digits(fraction)? * 10_i64.pow(9 - fraction.len() as u32);
            (clock, nanos)
        }
        None => (clock, 0),
    };
    if clock.len() != 8 || &clock[2..3] != ":" || &clock[5..6] != ":" {
        return None;
    }
    let hour = digits(&clock[..2])?;
    let minute = digits(&clock[3..5])?;
    let second = digits(&clock[6..8])?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let seconds =
        i128::from(date.days()) * 86_400 + i128::from(hour * 3600 + minute * 60 + second - offset);
    i64::try_from(seconds * 1_000_000_000 + i128::from(fraction)).ok()
}

/// The default synthetic run window: 2024-01-01 to 2024-01-03 UTC.
pub const DEFAULT_START_NS: i64 = 19_723 * harness_trading::replay_errors::ONE_DAY_NS;
pub const DEFAULT_END_NS: i64 = 19_725 * harness_trading::replay_errors::ONE_DAY_NS;
