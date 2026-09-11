//! Exact, configuration-driven fee calculation and aggregation.
//!
//! Port of `harness.fills.fees` and `harness.runtime.backtest.fees`. Every
//! money value is a [`PyDecimal`] so the scale and rounding of the Python
//! `decimal.Decimal` reference are preserved byte for byte.

use std::collections::BTreeMap;

use num::{Context, PyDecimal, RoundingMode};

use crate::backtest::config::{BacktestConfig, FeeBasis, FeeConfig};
use crate::backtest::errors::BacktestError;

/// Default scale used to round derived fees.
pub const DEFAULT_FEE_PRECISION: &str = "0.0001";

/// Basis-point denominator.
fn basis_points() -> PyDecimal {
    PyDecimal::from_i128(10_000)
}

/// Rounding policy shared by every derived fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FeeRounding {
    /// Decimal place to round to.
    pub precision: PyDecimal,
    /// Rounding mode.
    pub mode: RoundingMode,
}

impl FeeRounding {
    /// Round one exact amount without introducing binary floating point.
    #[must_use]
    pub fn apply(self, amount: PyDecimal) -> PyDecimal {
        let context = Context::new(28, self.mode);
        amount
            .quantize_with(self.precision, &context)
            .unwrap_or(amount)
    }
}

/// One named, rounded part of a fill's total fee.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeComponent {
    /// Component identifier.
    pub fee_id: String,
    /// Rounded amount.
    pub amount: PyDecimal,
}

/// Itemized fees and attribution for one fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeAssessment {
    /// Fill identifier.
    pub fill_id: String,
    /// Schedule identifier.
    pub schedule_id: String,
    /// Currency code.
    pub currency: String,
    /// Run attribution.
    pub run_id: String,
    /// Strategy attribution.
    pub strategy_id: String,
    /// Asset class (`equity`, `option`, `crypto`).
    pub asset_class: String,
    /// Rounded components.
    pub components: Vec<FeeComponent>,
}

impl FeeAssessment {
    /// The exact sum of already-rounded components.
    #[must_use]
    pub fn total(&self) -> PyDecimal {
        let mut total = PyDecimal::ZERO;
        for component in &self.components {
            total = total.checked_add(component.amount).unwrap_or(total);
        }
        total
    }
}

/// Per-share commission plus sell-side SEC and FINRA TAF charges.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EquityRegulatoryFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Per-share commission.
    pub commission_per_share: PyDecimal,
    /// Minimum commission.
    pub commission_minimum: PyDecimal,
    /// SEC notional-bps rate.
    pub sec_rate_bps: PyDecimal,
    /// TAF per share.
    pub taf_per_share: PyDecimal,
    /// TAF maximum.
    pub taf_maximum: PyDecimal,
}

/// Options per-contract fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OptionsPerContractFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Per-contract commission.
    pub commission_per_contract: PyDecimal,
    /// Flat per-contract fee.
    pub fee_per_contract: PyDecimal,
}

/// Crypto maker/taker fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CryptoMakerTakerFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Maker notional-bps rate.
    pub maker_rate_bps: PyDecimal,
    /// Taker notional-bps rate.
    pub taker_rate_bps: PyDecimal,
}

/// The complete derived fee schedule for one run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeSchedule {
    /// Schedule identifier.
    pub schedule_id: String,
    /// ISO-4217 currency code.
    pub currency: String,
    /// Equity regulatory model.
    pub equity: EquityRegulatoryFeeModel,
    /// Options per-contract model.
    pub option: OptionsPerContractFeeModel,
    /// Crypto maker/taker model.
    pub crypto: CryptoMakerTakerFeeModel,
}

/// The fill inputs a fee assessment needs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FeeFill<'a> {
    /// Fill identifier.
    pub fill_id: &'a str,
    /// `buy` or `sell`.
    pub side: &'a str,
    /// String-exact quantity.
    pub quantity: PyDecimal,
    /// String-exact price.
    pub price: PyDecimal,
    /// Optional liquidity role.
    pub liquidity: Option<&'a str>,
}

impl FeeSchedule {
    /// Calculate rounded components for one fill with explicit attribution.
    #[must_use]
    pub fn calculate(
        &self,
        fill: &FeeFill<'_>,
        run_id: &str,
        strategy_id: &str,
        asset_class: &str,
    ) -> FeeAssessment {
        let components = match asset_class {
            "option" => {
                let quantity = fill.quantity;
                let commission = self.option.rounding.apply(
                    quantity
                        .checked_mul(self.option.commission_per_contract)
                        .unwrap_or(quantity),
                );
                let fee = self.option.rounding.apply(
                    quantity
                        .checked_mul(self.option.fee_per_contract)
                        .unwrap_or(quantity),
                );
                vec![
                    FeeComponent {
                        fee_id: "contract-commission".to_string(),
                        amount: commission,
                    },
                    FeeComponent {
                        fee_id: "contract-fee".to_string(),
                        amount: fee,
                    },
                ]
            }
            "crypto" => {
                let rate = if fill.liquidity == Some("maker") {
                    self.crypto.maker_rate_bps
                } else {
                    self.crypto.taker_rate_bps
                };
                let notional = fill
                    .quantity
                    .checked_mul(fill.price)
                    .and_then(|value| value.checked_div(basis_points()))
                    .and_then(|value| value.checked_mul(rate))
                    .unwrap_or(PyDecimal::ZERO);
                let role = if fill.liquidity == Some("maker") {
                    "maker"
                } else {
                    "taker"
                };
                vec![FeeComponent {
                    fee_id: format!("{role}-fee"),
                    amount: self.crypto.rounding.apply(notional),
                }]
            }
            _ => {
                let equity = &self.equity;
                let commission = fill
                    .quantity
                    .checked_mul(equity.commission_per_share)
                    .unwrap_or(PyDecimal::ZERO);
                let commission = max_decimal(commission, equity.commission_minimum);
                let mut components = vec![FeeComponent {
                    fee_id: "commission".to_string(),
                    amount: equity.rounding.apply(commission),
                }];
                if fill.side == "sell" {
                    let sec = fill
                        .quantity
                        .checked_mul(fill.price)
                        .and_then(|value| value.checked_mul(equity.sec_rate_bps))
                        .and_then(|value| value.checked_div(basis_points()))
                        .unwrap_or(PyDecimal::ZERO);
                    let taf = min_decimal(
                        fill.quantity
                            .checked_mul(equity.taf_per_share)
                            .unwrap_or(PyDecimal::ZERO),
                        equity.taf_maximum,
                    );
                    components.push(FeeComponent {
                        fee_id: "sec-regulatory".to_string(),
                        amount: equity.rounding.apply(sec),
                    });
                    components.push(FeeComponent {
                        fee_id: "finra-taf".to_string(),
                        amount: equity.rounding.apply(taf),
                    });
                }
                components
            }
        };
        FeeAssessment {
            fill_id: fill.fill_id.to_string(),
            schedule_id: self.schedule_id.clone(),
            currency: self.currency.clone(),
            run_id: run_id.to_string(),
            strategy_id: strategy_id.to_string(),
            asset_class: asset_class.to_string(),
            components,
        }
    }
}

/// One exact currency total for an aggregation key.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeAggregate {
    /// Aggregation key.
    pub key: String,
    /// Currency code.
    pub currency: String,
    /// Total amount.
    pub amount: PyDecimal,
}

/// Deterministic fee totals across all required attribution dimensions.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FeeAggregation {
    /// Totals by run.
    pub by_run: Vec<FeeAggregate>,
    /// Totals by strategy.
    pub by_strategy: Vec<FeeAggregate>,
    /// Totals by asset class.
    pub by_asset_class: Vec<FeeAggregate>,
}

/// Aggregate assessments by run, strategy, and asset class and currency.
#[must_use]
pub fn aggregate_fees(assessments: &[FeeAssessment]) -> FeeAggregation {
    let mut by_run: BTreeMap<(String, String), PyDecimal> = BTreeMap::new();
    let mut by_strategy: BTreeMap<(String, String), PyDecimal> = BTreeMap::new();
    let mut by_asset_class: BTreeMap<(String, String), PyDecimal> = BTreeMap::new();
    for assessment in assessments {
        let total = assessment.total();
        add_total(&mut by_run, &assessment.run_id, &assessment.currency, total);
        add_total(
            &mut by_strategy,
            &assessment.strategy_id,
            &assessment.currency,
            total,
        );
        add_total(
            &mut by_asset_class,
            &assessment.asset_class,
            &assessment.currency,
            total,
        );
    }
    FeeAggregation {
        by_run: aggregates(by_run),
        by_strategy: aggregates(by_strategy),
        by_asset_class: aggregates(by_asset_class),
    }
}

fn add_total(
    totals: &mut BTreeMap<(String, String), PyDecimal>,
    key: &str,
    currency: &str,
    amount: PyDecimal,
) {
    let entry = totals
        .entry((key.to_string(), currency.to_string()))
        .or_insert(PyDecimal::ZERO);
    *entry = entry.checked_add(amount).unwrap_or(*entry);
}

fn aggregates(totals: BTreeMap<(String, String), PyDecimal>) -> Vec<FeeAggregate> {
    totals
        .into_iter()
        .map(|((key, currency), amount)| FeeAggregate {
            key,
            currency,
            amount,
        })
        .collect()
}

/// Derive a fee schedule from configuration, mirroring `fee_schedule_from_config`.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] when the precision or rounding mode
/// is invalid.
pub fn fee_schedule_from_config(
    fees: &FeeConfig,
    precision: &str,
    schedule_id: &str,
) -> Result<FeeSchedule, BacktestError> {
    let precision: PyDecimal = precision.parse().map_err(|_| {
        BacktestError::Configuration(format!("precision must be an exact decimal: {precision}"))
    })?;
    let mode = RoundingMode::from_name(&fees.rounding).ok_or_else(|| {
        BacktestError::Configuration(format!("unknown rounding mode {:?}", fees.rounding))
    })?;
    let rounding = FeeRounding { precision, mode };
    let per_unit = sum_rates(fees, FeeBasis::PerUnit);
    let flat = sum_rates(fees, FeeBasis::Flat);
    let notional_bps = sum_rates(fees, FeeBasis::NotionalBps);
    Ok(FeeSchedule {
        schedule_id: schedule_id.to_string(),
        currency: fees.currency.clone(),
        equity: EquityRegulatoryFeeModel {
            rounding,
            commission_per_share: per_unit,
            commission_minimum: flat,
            sec_rate_bps: notional_bps,
            taf_per_share: PyDecimal::ZERO,
            taf_maximum: PyDecimal::ZERO,
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

fn sum_rates(fees: &FeeConfig, basis: FeeBasis) -> PyDecimal {
    let mut total = PyDecimal::ZERO;
    for rule in fees.rules.iter().filter(|rule| rule.basis == basis) {
        total = total.checked_add(rule.rate).unwrap_or(total);
    }
    total
}

fn max_decimal(left: PyDecimal, right: PyDecimal) -> PyDecimal {
    if left.cmp_value(right) == std::cmp::Ordering::Less {
        right
    } else {
        left
    }
}

fn min_decimal(left: PyDecimal, right: PyDecimal) -> PyDecimal {
    if left.cmp_value(right) == std::cmp::Ordering::Greater {
        right
    } else {
        left
    }
}

#[cfg(test)]
#[path = "../../tests/unit/backtest_fees.rs"]
mod tests;
