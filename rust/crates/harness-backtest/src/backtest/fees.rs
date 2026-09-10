//! Fee schedule derivation from harness backtest configuration.
//!
//! Port of `harness.runtime.backtest.fees`. Each configured `per_unit` rule
//! becomes the per-share or per-contract commission, each `flat` rule becomes
//! the commission minimum or per-contract fee, and each `notional_bps` rule
//! becomes the notional-basis-point component shared by the equity regulatory
//! and crypto maker/taker models.

use std::str::FromStr;

use harness_num::{Decimal18, RoundingMode};

use crate::backtest::config::{BacktestConfig, FeeBasis, FeeConfig};
use crate::backtest::errors::BacktestError;

/// Default scale used to round derived fees.
pub const DEFAULT_FEE_PRECISION: &str = "0.0001";

/// Rounding policy shared by every derived fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FeeRounding {
    /// Decimal places to round to.
    pub precision: Decimal18,
    /// Rounding mode.
    pub mode: RoundingMode,
}

/// Equity regulatory fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EquityRegulatoryFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Per-share commission.
    pub commission_per_share: Decimal18,
    /// Minimum commission.
    pub commission_minimum: Decimal18,
    /// SEC notional-bps rate.
    pub sec_rate_bps: Decimal18,
    /// TAF per share.
    pub taf_per_share: Decimal18,
    /// TAF maximum.
    pub taf_maximum: Decimal18,
}

/// Options per-contract fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OptionsPerContractFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Per-contract commission.
    pub commission_per_contract: Decimal18,
    /// Flat per-contract fee.
    pub fee_per_contract: Decimal18,
}

/// Crypto maker/taker fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CryptoMakerTakerFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Maker notional-bps rate.
    pub maker_rate_bps: Decimal18,
    /// Taker notional-bps rate.
    pub taker_rate_bps: Decimal18,
}

/// The complete derived fee schedule for one run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FeeSchedule {
    /// Schedule identifier.
    pub schedule_id: &'static str,
    /// ISO-4217 currency code.
    pub currency: CurrencyCode,
    /// Equity regulatory model.
    pub equity: EquityRegulatoryFeeModel,
    /// Options per-contract model.
    pub option: OptionsPerContractFeeModel,
    /// Crypto maker/taker model.
    pub crypto: CryptoMakerTakerFeeModel,
}

/// A short currency code carried by value so [`FeeSchedule`] stays `Copy`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CurrencyCode {
    bytes: [u8; 3],
}

impl CurrencyCode {
    /// Parse a three-letter uppercase currency code.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        let bytes = value.as_bytes();
        if bytes.len() == 3 && bytes.iter().all(u8::is_ascii_uppercase) {
            Some(Self {
                bytes: [bytes[0], bytes[1], bytes[2]],
            })
        } else {
            None
        }
    }

    /// The currency code as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes).unwrap_or("USD")
    }
}

/// Derive a fee schedule from configuration.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] when a rule rate is not an exact
/// decimal or the currency code is malformed.
pub fn fee_schedule_from_config(
    fees: &FeeConfig,
    precision: &str,
    schedule_id: &'static str,
) -> Result<FeeSchedule, BacktestError> {
    let precision = parse_decimal(precision, "precision")?;
    let rounding = FeeRounding {
        precision,
        mode: RoundingMode::from_name(&fees.rounding).ok_or_else(|| {
            BacktestError::Configuration(format!("unknown rounding mode {:?}", fees.rounding))
        })?,
    };
    let per_unit = sum_rates(fees, FeeBasis::PerUnit)?;
    let flat = sum_rates(fees, FeeBasis::Flat)?;
    let notional_bps = sum_rates(fees, FeeBasis::NotionalBps)?;
    let currency = CurrencyCode::parse(&fees.currency).ok_or_else(|| {
        BacktestError::Configuration(format!("invalid currency code {:?}", fees.currency))
    })?;
    Ok(FeeSchedule {
        schedule_id,
        currency,
        equity: EquityRegulatoryFeeModel {
            rounding,
            commission_per_share: per_unit,
            commission_minimum: flat,
            sec_rate_bps: notional_bps,
            taf_per_share: Decimal18::ZERO,
            taf_maximum: Decimal18::ZERO,
        },
        option: OptionsPerContractFeeModel {
            rounding,
            commission_per_contract: per_unit,
            fee_per_contract: flat,
        },
        crypto: CryptoMakerTakerFeeModel {
            rounding,
            maker_rate_bps: notional_bps,
            taker_rate_bps: notional_bps,
        },
    })
}

/// Derive a schedule from a full backtest configuration.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] on the same failures as
/// [`fee_schedule_from_config`].
pub fn fee_schedule_for_run(config: &BacktestConfig) -> Result<FeeSchedule, BacktestError> {
    fee_schedule_from_config(&config.fees, DEFAULT_FEE_PRECISION, "harness-config")
}

fn sum_rates(fees: &FeeConfig, basis: FeeBasis) -> Result<Decimal18, BacktestError> {
    let mut total = Decimal18::ZERO;
    for rule in fees.rules.iter().filter(|rule| rule.basis == basis) {
        let rate = parse_decimal(&rule.rate, "fee rate")?;
        total = total
            .checked_add(rate)
            .map_err(|error| BacktestError::Configuration(error.to_string()))?;
    }
    Ok(total)
}

fn parse_decimal(value: &str, field: &str) -> Result<Decimal18, BacktestError> {
    Decimal18::from_str(value).map_err(|error| {
        BacktestError::Configuration(format!("{field} must be an exact decimal: {error}"))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backtest::config::FeeRule;

    fn config_with(rules: Vec<FeeRule>) -> FeeConfig {
        FeeConfig {
            currency: "USD".to_string(),
            rounding: "half_even".to_string(),
            rules,
        }
    }

    #[test]
    fn derives_all_three_models_from_bases() {
        let fees = config_with(vec![
            FeeRule {
                basis: FeeBasis::PerUnit,
                rate: "0.005".to_string(),
            },
            FeeRule {
                basis: FeeBasis::Flat,
                rate: "1".to_string(),
            },
            FeeRule {
                basis: FeeBasis::NotionalBps,
                rate: "0.0000278".to_string(),
            },
        ]);
        let schedule = fee_schedule_from_config(&fees, DEFAULT_FEE_PRECISION, "harness-config")
            .expect("schedule");
        assert_eq!(schedule.schedule_id, "harness-config");
        assert_eq!(schedule.currency.as_str(), "USD");
        assert_eq!(
            schedule.equity.commission_per_share.to_string(),
            "0.005000000000000000"
        );
        assert_eq!(
            schedule.option.commission_per_contract.to_string(),
            "0.005000000000000000"
        );
        assert_eq!(
            schedule.crypto.taker_rate_bps.to_string(),
            "0.000027800000000000"
        );
    }

    #[test]
    fn sums_repeated_bases() {
        let fees = config_with(vec![
            FeeRule {
                basis: FeeBasis::PerUnit,
                rate: "0.001".to_string(),
            },
            FeeRule {
                basis: FeeBasis::PerUnit,
                rate: "0.002".to_string(),
            },
        ]);
        let schedule = fee_schedule_from_config(&fees, "0.0001", "s").expect("schedule");
        assert_eq!(
            schedule.equity.commission_per_share.to_string(),
            "0.003000000000000000"
        );
        assert_eq!(schedule.option.fee_per_contract, Decimal18::ZERO);
    }

    #[test]
    fn rejects_bad_rates_and_currency() {
        let bad_rate = config_with(vec![FeeRule {
            basis: FeeBasis::Flat,
            rate: "not-a-number".to_string(),
        }]);
        assert!(fee_schedule_from_config(&bad_rate, "0.0001", "s").is_err());
        let bad_currency = FeeConfig {
            currency: "usd".to_string(),
            ..FeeConfig::default()
        };
        assert!(fee_schedule_from_config(&bad_currency, "0.0001", "s").is_err());
    }
}
