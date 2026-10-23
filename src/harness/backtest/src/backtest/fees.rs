//! Exact, configuration-driven fee calculation and aggregation.
//!
//! Port of `harness.fills.fees` and `harness.runtime.backtest.fees`. Every
//! money value is a [`Decimal`] so the exact scale and rounding contract is
//! preserved byte for byte.

use std::collections::BTreeMap;

use num::Context;
use num::Decimal;
use num::DecimalError;
use num::RoundingMode;

use crate::backtest::config::BacktestConfig;
use crate::backtest::config::FeeBasis;
use crate::backtest::config::FeeConfig;
use harness_trading::replay_errors::BacktestError;

/// Default scale used to round derived fees.
pub const DEFAULT_FEE_PRECISION: &str = "0.0001";

/// Basis-point denominator.
fn basis_points() -> Decimal {
    Decimal::from_i128(10_000)
}

/// Rounding policy shared by every derived fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FeeRounding {
    /// Decimal place to round to.
    precision: Decimal,
    /// Rounding mode.
    mode: RoundingMode,
}

impl FeeRounding {
    /// Construct a positive power-of-ten rounding increment.
    pub fn new(precision: Decimal, mode: RoundingMode) -> Result<Self, BacktestError> {
        let mut coefficient = precision.coefficient();
        while coefficient > 1 && coefficient.is_multiple_of(10) {
            coefficient /= 10;
        }
        if !precision.is_positive() || coefficient != 1 {
            return Err(BacktestError::Configuration(
                "fee precision must be a positive power of ten".into(),
            ));
        }
        Ok(Self { precision, mode })
    }
    /// Round one exact amount without introducing binary floating point.
    pub fn apply(self, amount: Decimal) -> Result<Decimal, BacktestError> {
        let context = Context::new(28, self.mode);
        amount
            .quantize_with(self.precision, &context)
            .map_err(|e| BacktestError::Arithmetic(format!("fee rounding failed: {e}")))
    }
}

/// One named, rounded part of a fill's total fee.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeComponent {
    /// Component identifier.
    pub fee_id: String,
    /// Rounded amount.
    pub amount: Decimal,
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
    pub job_id: String,
    /// Strategy attribution.
    pub strategy_id: String,
    /// Asset class (`equity`, `option`, `crypto`).
    pub asset_class: String,
    /// Rounded components.
    components: Vec<FeeComponent>,
    total: Decimal,
}

impl FeeAssessment {
    /// The exact sum of already-rounded components.
    #[must_use]
    pub fn total(&self) -> Decimal {
        self.total
    }

    pub fn components(&self) -> &[FeeComponent] {
        &self.components
    }
}

/// Per-share commission plus sell-side SEC and FINRA TAF charges.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EquityRegulatoryFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Per-share commission.
    pub commission_per_share: Decimal,
    /// Minimum commission.
    pub commission_minimum: Decimal,
    /// SEC notional-bps rate.
    pub sec_rate_bps: Decimal,
    /// TAF per share.
    pub taf_per_share: Decimal,
    /// TAF maximum.
    pub taf_maximum: Decimal,
}

/// Options per-contract fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OptionsPerContractFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Per-contract commission.
    pub commission_per_contract: Decimal,
    /// Flat per-contract fee.
    pub fee_per_contract: Decimal,
}

/// Crypto maker/taker fee model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CryptoMakerTakerFeeModel {
    /// Rounding policy.
    pub rounding: FeeRounding,
    /// Maker notional-bps rate.
    pub maker_rate_bps: Decimal,
    /// Taker notional-bps rate.
    pub taker_rate_bps: Decimal,
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
    pub quantity: Decimal,
    /// String-exact price.
    pub price: Decimal,
    /// Optional liquidity role.
    pub liquidity: Option<&'a str>,
}

impl FeeSchedule {
    /// Calculate rounded components for one fill with explicit attribution.
    ///
    /// # Errors
    ///
    /// Returns [`BacktestError::Arithmetic`] when a component product cannot be
    /// represented exactly, so the fee is never silently zeroed.
    pub fn calculate(
        &self,
        fill: &FeeFill<'_>,
        job_id: &str,
        strategy_id: &str,
        asset_class: &str,
    ) -> Result<FeeAssessment, BacktestError> {
        let (raw, rounding) = match asset_class {
            "option" => (
                harness_trading::fee_policy::options(
                    fill.quantity,
                    self.option.commission_per_contract,
                    self.option.fee_per_contract,
                ),
                self.option.rounding,
            ),
            "crypto" => {
                let maker = match fill.liquidity {
                    Some("maker") => true,
                    Some("taker") => false,
                    _ => {
                        return Err(BacktestError::Configuration(
                            "crypto fills require maker or taker liquidity".into(),
                        ))
                    }
                };
                (
                    harness_trading::fee_policy::crypto(
                        fill.quantity,
                        fill.price,
                        if maker {
                            self.crypto.maker_rate_bps
                        } else {
                            self.crypto.taker_rate_bps
                        },
                        maker,
                        basis_points(),
                    ),
                    self.crypto.rounding,
                )
            }
            _ => (
                harness_trading::fee_policy::equity(
                    fill.quantity,
                    fill.price,
                    fill.side == "sell",
                    harness_trading::fee_policy::EquityRates {
                        commission_per_share: self.equity.commission_per_share,
                        commission_minimum: self.equity.commission_minimum,
                        sec_rate_bps: self.equity.sec_rate_bps,
                        taf_per_share: self.equity.taf_per_share,
                        taf_maximum: self.equity.taf_maximum,
                    },
                    basis_points(),
                ),
                self.equity.rounding,
            ),
        };
        let components = raw
            .map_err(|error| match error {
                harness_trading::fee_policy::FeeError::Arithmetic(component, source) => {
                    fee_overflow(fill.fill_id, component, source)
                }
                harness_trading::fee_policy::FeeError::FractionalContracts => {
                    BacktestError::Configuration(error.to_string())
                }
            })?
            .into_iter()
            .map(|(fee_id, amount)| {
                Ok(FeeComponent {
                    fee_id: fee_id.to_string(),
                    amount: rounding.apply(amount)?,
                })
            })
            .collect::<Result<Vec<_>, BacktestError>>()?;
        let total = components
            .iter()
            .try_fold(Decimal::ZERO, |total, component| {
                total.checked_add(component.amount)
            })
            .map_err(|e| {
                BacktestError::Arithmetic(format!("fee total cannot be represented: {e}"))
            })?;
        Ok(FeeAssessment {
            fill_id: fill.fill_id.to_string(),
            schedule_id: self.schedule_id.clone(),
            currency: self.currency.clone(),
            job_id: job_id.to_string(),
            strategy_id: strategy_id.to_string(),
            asset_class: asset_class.to_string(),
            components,
            total,
        })
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
    pub amount: Decimal,
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
pub fn aggregate_fees(assessments: &[FeeAssessment]) -> Result<FeeAggregation, BacktestError> {
    let mut by_run: BTreeMap<(String, String), Decimal> = BTreeMap::new();
    let mut by_strategy: BTreeMap<(String, String), Decimal> = BTreeMap::new();
    let mut by_asset_class: BTreeMap<(String, String), Decimal> = BTreeMap::new();
    for assessment in assessments {
        let total = assessment.total();
        add_total(&mut by_run, &assessment.job_id, &assessment.currency, total)?;
        add_total(
            &mut by_strategy,
            &assessment.strategy_id,
            &assessment.currency,
            total,
        )?;
        add_total(
            &mut by_asset_class,
            &assessment.asset_class,
            &assessment.currency,
            total,
        )?;
    }
    Ok(FeeAggregation {
        by_run: aggregates(by_run),
        by_strategy: aggregates(by_strategy),
        by_asset_class: aggregates(by_asset_class),
    })
}

fn add_total(
    totals: &mut BTreeMap<(String, String), Decimal>,
    key: &str,
    currency: &str,
    amount: Decimal,
) -> Result<(), BacktestError> {
    let entry = totals
        .entry((key.to_string(), currency.to_string()))
        .or_insert(Decimal::ZERO);
    *entry = entry
        .checked_add(amount)
        .map_err(|e| BacktestError::Arithmetic(format!("fee aggregate overflow: {e}")))?;
    Ok(())
}

fn aggregates(totals: BTreeMap<(String, String), Decimal>) -> Vec<FeeAggregate> {
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
    let precision: Decimal = precision.parse().map_err(|_| {
        BacktestError::Configuration(format!("precision must be an exact decimal: {precision}"))
    })?;
    let mode = RoundingMode::from_name(&fees.rounding).ok_or_else(|| {
        BacktestError::Configuration(format!("unknown rounding mode {:?}", fees.rounding))
    })?;
    let rounding = FeeRounding::new(precision, mode)?;
    let per_unit = sum_rates(fees, FeeBasis::PerUnit)?;
    let flat = sum_rates(fees, FeeBasis::Flat)?;
    let notional_bps = sum_rates(fees, FeeBasis::NotionalBps)?;
    Ok(FeeSchedule {
        schedule_id: schedule_id.to_string(),
        currency: fees.currency.clone(),
        equity: EquityRegulatoryFeeModel {
            rounding,
            commission_per_share: per_unit,
            commission_minimum: flat,
            sec_rate_bps: notional_bps,
            taf_per_share: Decimal::ZERO,
            taf_maximum: Decimal::ZERO,
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

fn sum_rates(fees: &FeeConfig, basis: FeeBasis) -> Result<Decimal, BacktestError> {
    let mut total = Decimal::ZERO;
    for rule in fees.rules.iter().filter(|rule| rule.basis == basis) {
        total = total
            .checked_add(rule.rate)
            .map_err(|e| BacktestError::Arithmetic(format!("fee rate sum overflow: {e}")))?;
    }
    Ok(total)
}

/// The named failure for one fee component whose exact product overflows.
fn fee_overflow(fill_id: &str, component: &str, error: DecimalError) -> BacktestError {
    BacktestError::Arithmetic(format!(
        "{component} for fill {fill_id} cannot be represented exactly: {error}"
    ))
}
