//! Minimal typed backtest configuration seam.
//!
//! The full Python loader in `harness.configuration` is owned by another
//! migration epic. The backtest kernel only needs the resolved fields it reads,
//! so this module defines a narrow, validated [`BacktestConfig`] that captures
//! those fields (including the fee rules and strategy identities used by the
//! manifest) without pulling in the whole schema graph.

use std::collections::BTreeMap;

use crate::canonical_json::JsonValue;
use crate::backtest::errors::BacktestError;

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
    /// Whether the rule is flat, per-unit, or notional-bps.
    pub basis: FeeBasis,
    /// The string-exact rate.
    pub rate: String,
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

/// The resolved fields the backtest kernel reads from configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestConfig {
    /// Run identifier.
    pub run_id: String,
    /// Configuration shell (`backtest`).
    pub shell: String,
    /// Schema version.
    pub schema_version: u64,
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
    /// Data snapshot identifier.
    pub data_snapshot_id: String,
    /// Data fingerprint.
    pub data_fingerprint: String,
    /// Configured symbols.
    pub symbols: Vec<String>,
    /// Fee policy.
    pub fees: FeeConfig,
    /// Configured strategy instances.
    pub strategy_instances: Vec<StrategyInstanceConfig>,
}

impl BacktestConfig {
    /// Build a validated configuration.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Configuration`] when the window is empty or the
    /// shell is not `backtest`.
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
            seed,
            start_ns,
            end_ns,
            base_currency: "USD".to_string(),
            initial_cash: "1000000".to_string(),
            data_snapshot_id: "synthetic".to_string(),
            data_fingerprint: String::new(),
            symbols: vec!["SPY".to_string()],
            fees: FeeConfig::default(),
            strategy_instances: Vec::new(),
        })
    }

    /// Whether the configuration drives a backtest shell.
    #[must_use]
    pub fn is_backtest(&self) -> bool {
        self.shell == "backtest"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_an_empty_window() {
        assert!(BacktestConfig::new("run", 10, 10, 1).is_err());
        assert!(BacktestConfig::new("run", 20, 10, 1).is_err());
    }

    #[test]
    fn defaults_match_python_backtest_shell() {
        let config = BacktestConfig::new("run", 0, 1, 7).expect("config");
        assert!(config.is_backtest());
        assert_eq!(config.base_currency, "USD");
        assert_eq!(config.schema_version, 1);
    }

    #[test]
    fn fee_basis_round_trips() {
        for basis in [FeeBasis::Flat, FeeBasis::PerUnit, FeeBasis::NotionalBps] {
            assert_eq!(FeeBasis::parse(basis.value()), Some(basis));
        }
    }
}
