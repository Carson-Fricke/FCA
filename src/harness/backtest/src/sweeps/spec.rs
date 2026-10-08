//! Sweep specifications: deduplicated parameter spaces with canonical identity.
//!
//! Port of `harness.sweeps.spec`. A spec pairs one base configuration with an
//! explicit parameter space over one configured strategy instance and is
//! expanded either as a full Cartesian grid or as a seeded-random sample. Grid
//! expansion yields the product over parameter names in sorted order; random
//! expansion draws from the CPython-compatible
//! [`PythonRandom`](crate::backtest::rng::PythonRandom), so both modes are
//! reproducible. Points whose derived configuration hash repeats an earlier
//! point collapse, keeping dense indices.

use std::collections::BTreeMap;

use crate::backtest::config::BacktestConfig;
use crate::backtest::rng::PythonRandom;
use crate::canonical_json::JsonValue;
use crate::sha256::{digest_hex, Sha256};

/// Domain prefix for the sweep identity hash.
const SWEEP_DOMAIN: &[u8] = b"fca:harness-sweeps:spec:v1\0";
/// Sweep spec version tag.
pub const SWEEP_VERSION: &str = "harness.sweeps.spec.v1";
const IDENTIFIER_MAX: usize = 128;
const RUN_ID_DIGEST: usize = 12;

/// Parameter space sampling mode for one sweep.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SweepMode {
    /// Full Cartesian product.
    Grid,
    /// Seeded random sample.
    Random,
}

impl SweepMode {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Grid => "grid",
            Self::Random => "random",
        }
    }

    /// Parse a mode from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "grid" => Some(Self::Grid),
            "random" => Some(Self::Random),
            _ => None,
        }
    }
}

/// A sweep specification failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SweepError {
    /// The space was empty or a parameter had no values.
    EmptySpace(String),
    /// A parameter name was blank.
    BlankParameter,
    /// The instance id is not configured on the base.
    UnknownInstance(String),
    /// A random sweep requested fewer than one draw.
    InvalidCount,
    /// A random sweep omitted its seed (or a grid sweep supplied one).
    InvalidSeed,
}

impl std::fmt::Display for SweepError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptySpace(name) => {
                write!(
                    formatter,
                    "sweep parameter {name:?} must list at least one value"
                )
            }
            Self::BlankParameter => {
                formatter.write_str("sweep parameter names must be non-empty strings")
            }
            Self::UnknownInstance(id) => write!(
                formatter,
                "instance_id {id:?} is not a configured strategy instance"
            ),
            Self::InvalidCount => {
                formatter.write_str("random sweeps must request at least one draw")
            }
            Self::InvalidSeed => formatter.write_str("random sweeps require a recorded seed"),
        }
    }
}

impl std::error::Error for SweepError {}

/// One deduplicated sweep configuration and its derived identities.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepPoint {
    /// Dense first-occurrence index.
    pub index: usize,
    /// Parameter assignments, sorted by name.
    pub parameters: BTreeMap<String, JsonValue>,
    /// Canonical configuration digest of the derived configuration.
    pub configuration_digest: String,
    /// Digest-derived launched run identifier.
    pub job_id: String,
}

/// Reproducible parameter sweep over one strategy instance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SweepSpec {
    /// Base configuration.
    pub base: BacktestConfig,
    /// Swept strategy instance identifier.
    pub instance_id: String,
    /// Parameter names to candidate values, sorted by name.
    pub space: BTreeMap<String, Vec<JsonValue>>,
    /// Sampling mode.
    pub mode: SweepMode,
    /// Draw count (derived for grid mode).
    pub count: usize,
    /// Recorded seed for random mode.
    pub seed: Option<u64>,
}

impl SweepSpec {
    /// Build a full Cartesian-product sweep.
    ///
    /// # Errors
    ///
    /// Returns [`SweepError`] when the space is empty, a parameter has no
    /// values, a name is blank, or the instance is not configured.
    pub fn grid(
        base: BacktestConfig,
        instance_id: impl Into<String>,
        space: BTreeMap<String, Vec<JsonValue>>,
    ) -> Result<Self, SweepError> {
        let instance_id = instance_id.into();
        let space = canonical_space(space)?;
        validate_instance(&base, &instance_id)?;
        let count = space.values().map(Vec::len).product();
        Ok(Self {
            base,
            instance_id,
            space,
            mode: SweepMode::Grid,
            count,
            seed: None,
        })
    }

    /// Build a seeded-random sweep over the candidate values.
    ///
    /// # Errors
    ///
    /// Returns [`SweepError`] for the same reasons as [`SweepSpec::grid`],
    /// plus a zero draw count.
    pub fn random(
        base: BacktestConfig,
        instance_id: impl Into<String>,
        space: BTreeMap<String, Vec<JsonValue>>,
        count: usize,
        seed: u64,
    ) -> Result<Self, SweepError> {
        if count < 1 {
            return Err(SweepError::InvalidCount);
        }
        let instance_id = instance_id.into();
        let space = canonical_space(space)?;
        validate_instance(&base, &instance_id)?;
        Ok(Self {
            base,
            instance_id,
            space,
            mode: SweepMode::Random,
            count,
            seed: Some(seed),
        })
    }

    /// Return the canonical spec document backing the sweep identity.
    #[must_use]
    pub fn payload(&self) -> JsonValue {
        JsonValue::object([
            ("version", JsonValue::string(SWEEP_VERSION)),
            (
                "base",
                configuration_json(&self.base, &self.instance_id, None),
            ),
            ("instance_id", JsonValue::String(self.instance_id.clone())),
            ("mode", JsonValue::string(self.mode.value())),
            (
                "space",
                JsonValue::Object(
                    self.space
                        .iter()
                        .map(|(name, values)| (name.clone(), JsonValue::Array(values.clone())))
                        .collect(),
                ),
            ),
            ("count", JsonValue::UInt(self.count as u64)),
            (
                "seed",
                match self.seed {
                    Some(seed) => JsonValue::UInt(seed),
                    None => JsonValue::Null,
                },
            ),
        ])
    }

    /// Return the domain-separated canonical hash of the whole spec.
    #[must_use]
    pub fn sweep_id(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(SWEEP_DOMAIN);
        hasher.update(self.payload().to_canonical_string_utf8().as_bytes());
        hasher.finalize_hex()
    }

    /// Resolve the space into deduplicated points in first-occurrence order.
    #[must_use]
    pub fn expand(&self) -> Vec<SweepPoint> {
        let names: Vec<&String> = self.space.keys().collect();
        let mut points = Vec::new();
        let mut seen: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
        for combo in self.combinations() {
            let parameters: BTreeMap<String, JsonValue> = names
                .iter()
                .zip(combo.iter())
                .map(|(name, value)| ((*name).clone(), value.clone()))
                .collect();
            let digest = configuration_digest(&self.base, &self.instance_id, &parameters);
            if !seen.insert(digest.clone()) {
                continue;
            }
            let job_id = derive_job_id(&self.base.job_id, &digest);
            points.push(SweepPoint {
                index: points.len(),
                parameters,
                configuration_digest: digest,
                job_id,
            });
        }
        points
    }

    fn combinations(&self) -> Vec<Vec<JsonValue>> {
        match self.mode {
            SweepMode::Grid => {
                let mut result: Vec<Vec<JsonValue>> = vec![Vec::new()];
                for values in self.space.values() {
                    let mut next = Vec::new();
                    for prefix in &result {
                        for value in values {
                            let mut combo = prefix.clone();
                            combo.push(value.clone());
                            next.push(combo);
                        }
                    }
                    result = next;
                }
                result
            }
            SweepMode::Random => {
                let mut rng = PythonRandom::from_seed(self.seed.unwrap_or(0));
                let mut result = Vec::with_capacity(self.count);
                for _ in 0..self.count {
                    let mut combo = Vec::with_capacity(self.space.len());
                    for values in self.space.values() {
                        let index = rng.randbelow(values.len() as u32) as usize;
                        combo.push(values[index].clone());
                    }
                    result.push(combo);
                }
                result
            }
        }
    }
}

fn canonical_space(
    space: BTreeMap<String, Vec<JsonValue>>,
) -> Result<BTreeMap<String, Vec<JsonValue>>, SweepError> {
    if space.is_empty() {
        return Err(SweepError::EmptySpace(String::new()));
    }
    let mut canonical = BTreeMap::new();
    for (name, values) in space {
        if name.trim().is_empty() {
            return Err(SweepError::BlankParameter);
        }
        if values.is_empty() {
            return Err(SweepError::EmptySpace(name));
        }
        canonical.insert(name, values);
    }
    Ok(canonical)
}

fn validate_instance(base: &BacktestConfig, instance_id: &str) -> Result<(), SweepError> {
    if base
        .strategy_instances
        .iter()
        .any(|instance| instance.instance_id == instance_id)
    {
        Ok(())
    } else {
        Err(SweepError::UnknownInstance(instance_id.to_string()))
    }
}

fn merge_parameters(
    base: &BacktestConfig,
    instance_id: &str,
    parameters: Option<&BTreeMap<String, JsonValue>>,
) -> BTreeMap<String, JsonValue> {
    let mut merged = base
        .strategy_instances
        .iter()
        .find(|instance| instance.instance_id == instance_id)
        .map(|instance| instance.parameters.clone())
        .unwrap_or_default();
    if let Some(parameters) = parameters {
        for (key, value) in parameters {
            merged.insert(key.clone(), value.clone());
        }
    }
    merged
}

/// Canonical JSON for a configuration with one instance's parameters merged.
#[must_use]
pub fn configuration_json(
    base: &BacktestConfig,
    instance_id: &str,
    parameters: Option<&BTreeMap<String, JsonValue>>,
) -> JsonValue {
    let mut instances = Vec::new();
    for instance in &base.strategy_instances {
        let merged = merge_parameters(base, &instance.instance_id, None);
        let parameters = if instance.instance_id == instance_id {
            JsonValue::Object(merge_parameters(base, instance_id, parameters))
        } else {
            JsonValue::Object(merged)
        };
        instances.push(JsonValue::object([
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
            ("parameters", parameters),
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
                "capital_fraction",
                JsonValue::String(instance.capital_fraction.clone()),
            ),
        ]));
    }
    JsonValue::object([
        ("job_id", JsonValue::String(base.job_id.clone())),
        ("shell", JsonValue::String(base.shell.clone())),
        ("schema_version", JsonValue::UInt(base.schema_version)),
        ("seed", JsonValue::UInt(base.seed)),
        ("start_ns", JsonValue::Int(base.start_ns)),
        ("end_ns", JsonValue::Int(base.end_ns)),
        (
            "base_currency",
            JsonValue::String(base.base_currency.clone()),
        ),
        ("initial_cash", JsonValue::String(base.initial_cash.clone())),
        (
            "symbols",
            JsonValue::Array(
                base.symbols
                    .iter()
                    .map(|symbol| JsonValue::String(symbol.clone()))
                    .collect(),
            ),
        ),
        ("strategy_instances", JsonValue::Array(instances)),
    ])
}

/// Return the canonical configuration hash of one derived point.
#[must_use]
pub fn configuration_digest(
    base: &BacktestConfig,
    instance_id: &str,
    parameters: &BTreeMap<String, JsonValue>,
) -> String {
    harness_contracts::configuration_hash(
        &configuration_json(base, instance_id, Some(parameters)).to_serde_value(),
        harness_contracts::HashEncoding::Utf8,
    )
}

/// Derive one run identity from the base run ID and configuration digest.
#[must_use]
pub fn derive_job_id(base_job_id: &str, digest: &str) -> String {
    let stem_len = IDENTIFIER_MAX
        .saturating_sub(RUN_ID_DIGEST)
        .saturating_sub(1);
    let stem: String = base_job_id.chars().take(stem_len).collect();
    let digest_prefix: String = digest.chars().take(RUN_ID_DIGEST).collect();
    format!("{stem}-{digest_prefix}")
}

/// Compute a raw SHA-256 hex digest, exported for tests.
#[must_use]
pub fn raw_digest(bytes: &[u8]) -> String {
    digest_hex(bytes)
}
