//! Typed and canonical market-lake universe loaders.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{Map, Value};

use crate::env::{self, EnvKind};
use crate::error::ConfigError;
use crate::market_lake_config_dir;
use crate::yaml::load_yaml_mapping;

const ENV_SCHEMA: &[(&str, EnvKind)] = &[
    ("schema_version", EnvKind::Int),
    ("equities.soak.symbols", EnvKind::StrList),
    ("equities.soak.data_types", EnvKind::StrList),
    ("equities.soak.feed", EnvKind::Str),
    ("equities.soak.adjustment", EnvKind::Str),
    ("equities.soak.history.trades.policy", EnvKind::Str),
    (
        "equities.soak.history.trades.maximum_liquidity_rank",
        EnvKind::Uint,
    ),
    ("equities.soak.history.quotes.policy", EnvKind::Str),
    (
        "equities.soak.history.quotes.maximum_liquidity_rank",
        EnvKind::Uint,
    ),
    ("equities.soak.backfill_order", EnvKind::Str),
    ("options.feed", EnvKind::Str),
    ("options.data_types", EnvKind::StrList),
    ("options.selection.frequency", EnvKind::Str),
    ("options.selection.persist_front_set", EnvKind::Bool),
    ("options.selection.expiration_order", EnvKind::Str),
    ("options.selection.expiration_count", EnvKind::Uint),
    ("options.selection.strike_window.basis", EnvKind::Str),
    (
        "options.selection.strike_window.percent_below",
        EnvKind::Float,
    ),
    (
        "options.selection.strike_window.percent_above",
        EnvKind::Float,
    ),
    ("options.soak.underlyings", EnvKind::StrList),
    ("options.soak.lookback_years", EnvKind::Float),
    ("options.expansion.enabled", EnvKind::Bool),
    ("options.expansion.maximum_underlyings", EnvKind::Uint),
    ("crypto.pairs", EnvKind::StrList),
    ("crypto.data_types", EnvKind::StrList),
    ("crypto.rollout.enabled", EnvKind::Bool),
    ("crypto.rollout.target_pair_count", EnvKind::Uint),
    ("crypto.rollout.scheduling_priority", EnvKind::Str),
    ("crypto.rollout.quota_policy", EnvKind::Str),
];

/// Complete typed representation of `universes.yaml`.
#[derive(Clone, Debug, PartialEq)]
pub struct UniverseSettings {
    /// Configuration schema version; always 1.
    pub schema_version: i64,
    /// Equity universes.
    pub equities: EquityUniverses,
    /// Options universe.
    pub options: OptionsUniverse,
    /// Crypto universe.
    pub crypto: CryptoUniverse,
}

/// Equity universes by role.
#[derive(Clone, Debug, PartialEq)]
pub struct EquityUniverses {
    /// The soak universe used by local ingest.
    pub soak: EquityUniverse,
}

/// Equity universe definition.
#[derive(Clone, Debug, PartialEq)]
pub struct EquityUniverse {
    /// Canonical symbols, deduplicated, order preserved.
    pub symbols: Vec<String>,
    /// Normalized data types (`trade`/`quote`), deduplicated.
    pub data_types: Vec<String>,
    /// Feed; always `sip`.
    pub feed: String,
    /// Adjustment; always `raw`.
    pub adjustment: String,
    /// Per-data-type history policy.
    pub history: EquityHistory,
    /// Backfill order; always `newest_first`.
    pub backfill_order: String,
}

/// Per-data-type equity history policy.
#[derive(Clone, Debug, PartialEq)]
pub struct EquityHistory {
    /// Trade history policy.
    pub trade: HistoryPolicy,
    /// Quote history policy.
    pub quote: HistoryPolicy,
}

/// One history policy.
#[derive(Clone, Debug, PartialEq)]
pub struct HistoryPolicy {
    /// Always `all_available`.
    pub policy: String,
    /// Optional liquidity-rank ceiling.
    pub maximum_liquidity_rank: Option<u32>,
}

/// Options universe definition.
#[derive(Clone, Debug, PartialEq)]
pub struct OptionsUniverse {
    /// Feed; always `opra`.
    pub feed: String,
    /// Normalized data types.
    pub data_types: Vec<String>,
    /// Daily selection policy.
    pub selection: OptionSelection,
    /// Soak policy.
    pub soak: OptionsSoak,
    /// Expansion policy.
    pub expansion: OptionsExpansion,
}

/// Daily option selection policy.
#[derive(Clone, Debug, PartialEq)]
pub struct OptionSelection {
    /// Always `daily`.
    pub frequency: String,
    /// Whether selected front sets are persisted.
    pub persist_front_set: bool,
    /// Always `nearest`.
    pub expiration_order: String,
    /// Expirations per selection (1..=12).
    pub expiration_count: u32,
    /// Strike window around spot.
    pub strike_window: StrikeWindow,
}

/// Strike window around underlying spot.
#[derive(Clone, Debug, PartialEq)]
pub struct StrikeWindow {
    /// Always `underlying_spot`.
    pub basis: String,
    /// Percent below spot (0, 100].
    pub percent_below: f64,
    /// Percent above spot (0, 100].
    pub percent_above: f64,
}

/// Options soak policy.
#[derive(Clone, Debug, PartialEq)]
pub struct OptionsSoak {
    /// Canonical underlyings.
    pub underlyings: Vec<String>,
    /// Lookback years (0, 100].
    pub lookback_years: f64,
}

/// Options expansion policy.
#[derive(Clone, Debug, PartialEq)]
pub struct OptionsExpansion {
    /// Whether expansion is enabled.
    pub enabled: bool,
    /// Maximum underlyings (1..=100000).
    pub maximum_underlyings: u32,
}

/// Crypto universe definition.
#[derive(Clone, Debug, PartialEq)]
pub struct CryptoUniverse {
    /// Canonical `BASE/QUOTE` pairs.
    pub pairs: Vec<String>,
    /// Normalized data types.
    pub data_types: Vec<String>,
    /// Rollout policy.
    pub rollout: CryptoRollout,
}

/// Crypto rollout policy.
#[derive(Clone, Debug, PartialEq)]
pub struct CryptoRollout {
    /// Whether rollout is enabled.
    pub enabled: bool,
    /// Target pair count (1..=100000).
    pub target_pair_count: u32,
    /// Always `after_equities_and_options`.
    pub scheduling_priority: String,
    /// Always `shared_backfill_quota`.
    pub quota_policy: String,
}

impl UniverseSettings {
    /// Load `universes.yaml` with environment precedence.
    pub fn load(
        root: Option<&Path>,
        environ: &BTreeMap<String, String>,
    ) -> Result<Self, ConfigError> {
        let mut path = market_lake_config_dir(root, environ);
        path.push("universes.yaml");
        let mut document = load_yaml_mapping(&path)?;
        env::apply(&mut document, environ, ENV_SCHEMA)?;
        Self::from_value(document)
    }

    /// Load `universes.yaml` against the process environment.
    pub fn load_from_env(root: Option<&Path>) -> Result<Self, ConfigError> {
        let environ: BTreeMap<String, String> = std::env::vars().collect();
        Self::load(root, &environ)
    }

    fn from_value(value: Value) -> Result<Self, ConfigError> {
        let mut root = Section::new("", value)?;
        let schema_version = root.integer("schema_version")?;
        if schema_version != 1 {
            return Err(ConfigError::new("schema_version must be 1"));
        }
        let equities = EquityUniverses::from_section(root.object("equities")?)?;
        let options = OptionsUniverse::from_section(root.object("options")?)?;
        let crypto = CryptoUniverse::from_section(root.object("crypto")?)?;
        root.finish()?;
        Ok(Self {
            schema_version,
            equities,
            options,
            crypto,
        })
    }
}

impl EquityUniverses {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let soak = EquityUniverse::from_section(section.object("soak")?)?;
        section.finish()?;
        Ok(Self { soak })
    }
}

impl EquityUniverse {
    /// The first soak symbol, used as the default local ingest symbol.
    pub fn default_symbol(&self) -> Result<String, ConfigError> {
        self.symbols
            .first()
            .cloned()
            .ok_or_else(|| ConfigError::new("equities.soak.symbols must not be empty"))
    }

    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let symbols = normalize_symbols(section.string_list("symbols")?, "equities.soak.symbols")?;
        let data_types = normalize_data_types(
            section.string_list("data_types")?,
            "equities.soak.data_types",
        )?;
        let feed = section.string("feed")?;
        if feed != "sip" {
            return Err(ConfigError::new("equities.soak.feed must be sip"));
        }
        let adjustment = section.string("adjustment")?;
        if adjustment != "raw" {
            return Err(ConfigError::new("equities.soak.adjustment must be raw"));
        }
        let history = EquityHistory::from_section(section.object("history")?)?;
        let backfill_order = section.string("backfill_order")?;
        if backfill_order != "newest_first" {
            return Err(ConfigError::new(
                "equities.soak.backfill_order must be newest_first",
            ));
        }
        section.finish()?;
        if data_types
            .iter()
            .any(|data_type| data_type != "trade" && data_type != "quote")
        {
            return Err(ConfigError::new(
                "equity data_types must contain only trades or quotes",
            ));
        }
        Ok(Self {
            symbols,
            data_types,
            feed,
            adjustment,
            history,
            backfill_order,
        })
    }
}

impl EquityHistory {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let trade = HistoryPolicy::from_value(
            section.take("trades").unwrap_or(Value::Null),
            "equities.soak.history.trades",
        )?;
        let quote = HistoryPolicy::from_value(
            section.take("quotes").unwrap_or(Value::Null),
            "equities.soak.history.quotes",
        )?;
        section.finish()?;
        Ok(Self { trade, quote })
    }
}

impl HistoryPolicy {
    fn from_value(value: Value, path: &str) -> Result<Self, ConfigError> {
        if matches!(value, Value::Null) {
            return Err(ConfigError::new(
                "equity history must define both trades and quotes",
            ));
        }
        let mut section = Section::new(path, value)?;
        let policy = section.string("policy")?;
        if policy != "all_available" {
            return Err(ConfigError::new(format!(
                "{path}.policy must be all_available"
            )));
        }
        let maximum_liquidity_rank = match section.take("maximum_liquidity_rank") {
            None | Some(Value::Null) => None,
            Some(value) => {
                let rank = value.as_u64().ok_or_else(|| {
                    ConfigError::new(format!(
                        "{path}.maximum_liquidity_rank must be a positive integer"
                    ))
                })? as u32;
                if rank == 0 {
                    return Err(ConfigError::new(format!(
                        "{path}.maximum_liquidity_rank must be a positive integer"
                    )));
                }
                Some(rank)
            }
        };
        section.finish()?;
        Ok(Self {
            policy,
            maximum_liquidity_rank,
        })
    }
}

impl OptionsUniverse {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let feed = section.string("feed")?;
        if feed != "opra" {
            return Err(ConfigError::new("options.feed must be opra"));
        }
        let data_types =
            normalize_data_types(section.string_list("data_types")?, "options.data_types")?;
        let selection = OptionSelection::from_section(section.object("selection")?)?;
        let soak = OptionsSoak::from_section(section.object("soak")?)?;
        let expansion = OptionsExpansion::from_section(section.object("expansion")?)?;
        section.finish()?;
        Ok(Self {
            feed,
            data_types,
            selection,
            soak,
            expansion,
        })
    }
}

impl OptionSelection {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let frequency = section.string("frequency")?;
        if frequency != "daily" {
            return Err(ConfigError::new(
                "options.selection.frequency must be daily",
            ));
        }
        let persist_front_set = section.boolean("persist_front_set")?;
        let expiration_order = section.string("expiration_order")?;
        if expiration_order != "nearest" {
            return Err(ConfigError::new(
                "options.selection.expiration_order must be nearest",
            ));
        }
        let expiration_count = section.uint("expiration_count")? as u32;
        if !(1..=12).contains(&expiration_count) {
            return Err(ConfigError::new(
                "options.selection.expiration_count must be between 1 and 12",
            ));
        }
        let strike_window = StrikeWindow::from_section(section.object("strike_window")?)?;
        section.finish()?;
        Ok(Self {
            frequency,
            persist_front_set,
            expiration_order,
            expiration_count,
            strike_window,
        })
    }
}

impl StrikeWindow {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let basis = section.string("basis")?;
        if basis != "underlying_spot" {
            return Err(ConfigError::new(
                "options.selection.strike_window.basis must be underlying_spot",
            ));
        }
        let percent_below = section.float("percent_below")?;
        let percent_above = section.float("percent_above")?;
        section.finish()?;
        if !(percent_below > 0.0 && percent_below <= 100.0)
            || !(percent_above > 0.0 && percent_above <= 100.0)
        {
            return Err(ConfigError::new(
                "options.selection.strike_window percents must be greater than 0 and at most 100",
            ));
        }
        Ok(Self {
            basis,
            percent_below,
            percent_above,
        })
    }
}

impl OptionsSoak {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let underlyings = normalize_symbols(
            section.string_list("underlyings")?,
            "options.soak.underlyings",
        )?;
        let lookback_years = section.float("lookback_years")?;
        section.finish()?;
        if !(lookback_years > 0.0 && lookback_years <= 100.0) {
            return Err(ConfigError::new(
                "options.soak.lookback_years must be greater than 0 and at most 100",
            ));
        }
        Ok(Self {
            underlyings,
            lookback_years,
        })
    }
}

impl OptionsExpansion {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let enabled = section.boolean("enabled")?;
        let maximum_underlyings = section.uint("maximum_underlyings")? as u32;
        section.finish()?;
        if !(1..=100_000).contains(&maximum_underlyings) {
            return Err(ConfigError::new(
                "options.expansion.maximum_underlyings must be between 1 and 100000",
            ));
        }
        Ok(Self {
            enabled,
            maximum_underlyings,
        })
    }
}

impl CryptoUniverse {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let pairs = normalize_pairs(section.string_list("pairs")?)?;
        let data_types =
            normalize_data_types(section.string_list("data_types")?, "crypto.data_types")?;
        let rollout = CryptoRollout::from_section(section.object("rollout")?)?;
        section.finish()?;
        Ok(Self {
            pairs,
            data_types,
            rollout,
        })
    }
}

impl CryptoRollout {
    fn from_section(mut section: Section) -> Result<Self, ConfigError> {
        let enabled = section.boolean("enabled")?;
        let target_pair_count = section.uint("target_pair_count")? as u32;
        if !(1..=100_000).contains(&target_pair_count) {
            return Err(ConfigError::new(
                "crypto.rollout.target_pair_count must be between 1 and 100000",
            ));
        }
        let scheduling_priority = section.string("scheduling_priority")?;
        if scheduling_priority != "after_equities_and_options" {
            return Err(ConfigError::new(
                "crypto.rollout.scheduling_priority must be after_equities_and_options",
            ));
        }
        let quota_policy = section.string("quota_policy")?;
        if quota_policy != "shared_backfill_quota" {
            return Err(ConfigError::new(
                "crypto.rollout.quota_policy must be shared_backfill_quota",
            ));
        }
        section.finish()?;
        Ok(Self {
            enabled,
            target_pair_count,
            scheduling_priority,
            quota_policy,
        })
    }
}

fn normalize_data_types(raw: Vec<String>, field_name: &str) -> Result<Vec<String>, ConfigError> {
    if raw.is_empty() {
        return Err(ConfigError::new(format!("{field_name} must not be empty")));
    }
    let mut result = Vec::new();
    for item in raw {
        let normalized = match item.trim().to_ascii_lowercase().as_str() {
            "trade" | "trades" => "trade",
            "quote" | "quotes" => "quote",
            _ => {
                return Err(ConfigError::new(format!("unsupported data type: {item:?}")));
            }
        };
        if !result.iter().any(|existing| existing == normalized) {
            result.push(normalized.to_string());
        }
    }
    Ok(result)
}

fn normalize_symbols(raw: Vec<String>, field_name: &str) -> Result<Vec<String>, ConfigError> {
    let unique = unique_sequence(raw, field_name)?;
    for symbol in &unique {
        if !is_equity_symbol(symbol) {
            return Err(ConfigError::new(format!(
                "invalid equity symbol in {field_name}: {symbol:?}"
            )));
        }
        lake_contracts::symbol_filename(symbol)
            .map_err(|error| ConfigError::new(error.to_string()))?;
    }
    Ok(unique)
}

fn normalize_pairs(raw: Vec<String>) -> Result<Vec<String>, ConfigError> {
    let unique = unique_sequence(raw, "crypto.pairs")?;
    for pair in &unique {
        if !is_crypto_pair(pair) {
            return Err(ConfigError::new(format!("invalid crypto pair: {pair:?}")));
        }
        lake_contracts::symbol_filename(pair)
            .map_err(|error| ConfigError::new(error.to_string()))?;
    }
    Ok(unique)
}

fn unique_sequence(raw: Vec<String>, field_name: &str) -> Result<Vec<String>, ConfigError> {
    if raw.is_empty() {
        return Err(ConfigError::new(format!("{field_name} must not be empty")));
    }
    let mut result: Vec<String> = Vec::new();
    for item in raw {
        let normalized = item.trim().to_uppercase();
        if normalized.is_empty() {
            return Err(ConfigError::new(format!(
                "{field_name} entries must not be empty"
            )));
        }
        if !result.iter().any(|existing| existing == &normalized) {
            result.push(normalized);
        }
    }
    Ok(result)
}

fn is_equity_symbol(symbol: &str) -> bool {
    let mut characters = symbol.chars();
    let Some(first) = characters.next() else {
        return false;
    };
    first.is_ascii_uppercase()
        && characters
            .all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit() || ch == '.' || ch == '-')
}

fn is_crypto_pair(pair: &str) -> bool {
    let Some((base, quote)) = pair.split_once('/') else {
        return false;
    };
    !base.is_empty()
        && !quote.is_empty()
        && base
            .chars()
            .all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit())
        && quote
            .chars()
            .all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit())
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
            Value::Number(number) => number
                .as_i64()
                .ok_or_else(|| ConfigError::new(format!("{path} must be an integer"))),
            _ => Err(ConfigError::new(format!("{path} must be an integer"))),
        }
    }

    fn uint(&mut self, key: &str) -> Result<u64, ConfigError> {
        let path = self.child(key);
        match self.require(key)? {
            Value::Number(number) => number
                .as_u64()
                .ok_or_else(|| ConfigError::new(format!("{path} must be a non-negative integer"))),
            _ => Err(ConfigError::new(format!(
                "{path} must be a non-negative integer"
            ))),
        }
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
