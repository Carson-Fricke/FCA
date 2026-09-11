//! Exact, configuration-driven fee calculation and aggregation.

use std::collections::BTreeMap;

use num::{Decimal18, RoundingMode};

use crate::accounting::Fill;
use crate::error::{require_text, LiveError, LiveResult};
use crate::orders::Side;

const ZERO: Decimal18 = Decimal18::ZERO;
const BASIS_POINTS: i64 = 10_000;

/// Asset classes with distinct fee calculation rules.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum AssetClass {
    /// Equities.
    Equity,
    /// Listed options.
    Option,
    /// Crypto.
    Crypto,
}

impl AssetClass {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Equity => "equity",
            Self::Option => "option",
            Self::Crypto => "crypto",
        }
    }
}

/// Whether a crypto fill added or removed resting liquidity.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum LiquidityRole {
    /// Added resting liquidity.
    Maker,
    /// Removed resting liquidity.
    Taker,
}

impl LiquidityRole {
    /// Parse the canonical `maker`/`taker` spelling.
    pub fn parse(value: &str) -> LiveResult<Self> {
        match value {
            "maker" => Ok(Self::Maker),
            "taker" => Ok(Self::Taker),
            _ => Err(LiveError::Fee(
                "crypto fills require maker or taker liquidity".into(),
            )),
        }
    }
}

/// Supported deterministic decimal rounding modes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum FeeRoundingMode {
    /// Round half to even.
    HalfEven,
    /// Round half away from zero.
    HalfUp,
    /// Round toward positive infinity.
    Ceiling,
}

impl FeeRoundingMode {
    fn to_rounding(self) -> RoundingMode {
        match self {
            Self::HalfEven => RoundingMode::HalfEven,
            Self::HalfUp => RoundingMode::HalfUp,
            Self::Ceiling => RoundingMode::Ceiling,
        }
    }
}

/// Currency precision and rounding mode applied to each fee component.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FeeRounding {
    /// A power-of-ten increment such as `0.01`.
    pub precision: Decimal18,
    /// The rounding mode.
    pub mode: FeeRoundingMode,
    places: u32,
}

impl FeeRounding {
    /// Construct a validated rounding rule.
    pub fn new(precision: Decimal18, mode: FeeRoundingMode) -> LiveResult<Self> {
        let places = power_of_ten_places(precision)
            .ok_or_else(|| LiveError::Fee("rounding.precision must be a power of ten".into()))?;
        Ok(Self {
            precision,
            mode,
            places,
        })
    }

    /// Round one exact amount without introducing binary floating point.
    pub fn apply(&self, amount: Decimal18) -> Decimal18 {
        amount
            .round_to(self.mode.to_rounding(), self.places)
            .unwrap_or(amount)
    }
}

/// Engine-owned attribution supplied independently of strategy logic.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeContext {
    /// Run identifier.
    pub run_id: String,
    /// Strategy-instance identifier.
    pub strategy_id: String,
    /// The asset class selecting the fee model.
    pub asset_class: AssetClass,
}

impl FeeContext {
    /// Construct a validated context.
    pub fn new(run_id: &str, strategy_id: &str, asset_class: AssetClass) -> LiveResult<Self> {
        Ok(Self {
            run_id: require_text(run_id, "run_id")?,
            strategy_id: require_text(strategy_id, "strategy_id")?,
            asset_class,
        })
    }
}

/// One named, rounded part of a fill's total fee.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeComponent {
    /// Component identifier.
    pub fee_id: String,
    /// Rounded amount.
    pub amount: Decimal18,
}

impl FeeComponent {
    /// Construct a validated component.
    pub fn new(fee_id: &str, amount: Decimal18) -> LiveResult<Self> {
        Ok(Self {
            fee_id: identifier(fee_id, "fee_id")?,
            amount,
        })
    }
}

/// Itemized fees and attribution for one fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeAssessment {
    /// The assessed fill identifier.
    pub fill_id: String,
    /// The schedule identifier.
    pub schedule_id: String,
    /// The ISO currency code.
    pub currency: String,
    /// Attribution context.
    pub context: FeeContext,
    /// Itemized components.
    pub components: Vec<FeeComponent>,
}

impl FeeAssessment {
    /// Construct a validated assessment with unique component identifiers.
    pub fn new(
        fill_id: &str,
        schedule_id: &str,
        currency: &str,
        context: FeeContext,
        components: Vec<FeeComponent>,
    ) -> LiveResult<Self> {
        let mut seen = std::collections::HashSet::new();
        for component in &components {
            if !seen.insert(component.fee_id.clone()) {
                return Err(LiveError::Fee(
                    "fee component identifiers must be unique".into(),
                ));
            }
        }
        Ok(Self {
            fill_id: require_text(fill_id, "fill_id")?,
            schedule_id: identifier(schedule_id, "schedule_id")?,
            currency: currency_code(currency)?,
            context,
            components,
        })
    }

    /// The exact sum of already-rounded components.
    pub fn total(&self) -> Decimal18 {
        self.components
            .iter()
            .fold(ZERO, |sum, component| sum + component.amount)
    }

    /// The run attribution used by aggregation.
    pub fn run_id(&self) -> &str {
        &self.context.run_id
    }

    /// The strategy-instance attribution used by aggregation.
    pub fn strategy_id(&self) -> &str {
        &self.context.strategy_id
    }

    /// The asset class used to select the fee model.
    pub fn asset_class(&self) -> AssetClass {
        self.context.asset_class
    }
}

/// Per-share commission plus sell-side SEC and FINRA TAF charges.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EquityRegulatoryFeeModel {
    /// Rounding rule.
    pub rounding: FeeRounding,
    /// Per-share commission.
    pub commission_per_share: Decimal18,
    /// Minimum commission.
    pub commission_minimum: Decimal18,
    /// SEC rate in basis points.
    pub sec_rate_bps: Decimal18,
    /// FINRA TAF per share.
    pub taf_per_share: Decimal18,
    /// FINRA TAF cap.
    pub taf_maximum: Decimal18,
}

impl EquityRegulatoryFeeModel {
    /// Construct a validated equity fee model.
    pub fn new(
        rounding: FeeRounding,
        commission_per_share: Decimal18,
        commission_minimum: Decimal18,
        sec_rate_bps: Decimal18,
        taf_per_share: Decimal18,
        taf_maximum: Decimal18,
    ) -> LiveResult<Self> {
        for (value, field) in [
            (commission_per_share, "commission_per_share"),
            (commission_minimum, "commission_minimum"),
            (sec_rate_bps, "sec_rate_bps"),
            (taf_per_share, "taf_per_share"),
            (taf_maximum, "taf_maximum"),
        ] {
            if value.is_negative() {
                return Err(LiveError::Fee(format!("{field} must not be negative")));
            }
        }
        Ok(Self {
            rounding,
            commission_per_share,
            commission_minimum,
            sec_rate_bps,
            taf_per_share,
            taf_maximum,
        })
    }

    /// Calculate commission and applicable sell-side regulatory fees.
    pub fn calculate(&self, fill: &Fill) -> LiveResult<Vec<FeeComponent>> {
        let raw_commission = fill.quantity * self.commission_per_share;
        let commission = if raw_commission > self.commission_minimum {
            raw_commission
        } else {
            self.commission_minimum
        };
        let mut components = vec![FeeComponent::new(
            "commission",
            self.rounding.apply(commission),
        )?];
        if fill.side == Side::Sell {
            let sec_fee =
                fill.quantity * fill.price * self.sec_rate_bps / Decimal18::from_i64(BASIS_POINTS);
            let raw_taf = fill.quantity * self.taf_per_share;
            let taf_fee = if raw_taf < self.taf_maximum {
                raw_taf
            } else {
                self.taf_maximum
            };
            components.push(FeeComponent::new(
                "sec-regulatory",
                self.rounding.apply(sec_fee),
            )?);
            components.push(FeeComponent::new(
                "finra-taf",
                self.rounding.apply(taf_fee),
            )?);
        }
        Ok(components)
    }
}

/// Commission and exchange/regulatory charges assessed per contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OptionsPerContractFeeModel {
    /// Rounding rule.
    pub rounding: FeeRounding,
    /// Per-contract commission.
    pub commission_per_contract: Decimal18,
    /// Per-contract regulatory fee.
    pub fee_per_contract: Decimal18,
}

impl OptionsPerContractFeeModel {
    /// Construct a validated option fee model.
    pub fn new(
        rounding: FeeRounding,
        commission_per_contract: Decimal18,
        fee_per_contract: Decimal18,
    ) -> LiveResult<Self> {
        for (value, field) in [
            (commission_per_contract, "commission_per_contract"),
            (fee_per_contract, "fee_per_contract"),
        ] {
            if value.is_negative() {
                return Err(LiveError::Fee(format!("{field} must not be negative")));
            }
        }
        Ok(Self {
            rounding,
            commission_per_contract,
            fee_per_contract,
        })
    }

    /// Calculate exact per-contract charges for an integral fill quantity.
    pub fn calculate(&self, fill: &Fill) -> LiveResult<Vec<FeeComponent>> {
        if !is_integral(fill.quantity) {
            return Err(LiveError::Fee(
                "option fill quantity must be a whole number of contracts".into(),
            ));
        }
        Ok(vec![
            FeeComponent::new(
                "contract-commission",
                self.rounding
                    .apply(fill.quantity * self.commission_per_contract),
            )?,
            FeeComponent::new(
                "contract-fee",
                self.rounding.apply(fill.quantity * self.fee_per_contract),
            )?,
        ])
    }
}

/// Notional-basis-point fees selected from the fill's liquidity role.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CryptoMakerTakerFeeModel {
    /// Rounding rule.
    pub rounding: FeeRounding,
    /// Maker rate in basis points (may be negative for a rebate).
    pub maker_rate_bps: Decimal18,
    /// Taker rate in basis points.
    pub taker_rate_bps: Decimal18,
}

impl CryptoMakerTakerFeeModel {
    /// Construct a validated crypto fee model.
    pub fn new(
        rounding: FeeRounding,
        maker_rate_bps: Decimal18,
        taker_rate_bps: Decimal18,
    ) -> LiveResult<Self> {
        if taker_rate_bps.is_negative() {
            return Err(LiveError::Fee("taker_rate_bps must not be negative".into()));
        }
        Ok(Self {
            rounding,
            maker_rate_bps,
            taker_rate_bps,
        })
    }

    /// Calculate a maker charge/rebate or taker charge from fill notional.
    pub fn calculate(&self, fill: &Fill) -> LiveResult<Vec<FeeComponent>> {
        let Some(liquidity) = &fill.liquidity else {
            return Err(LiveError::Fee(
                "crypto fills require maker or taker liquidity".into(),
            ));
        };
        let role = LiquidityRole::parse(liquidity)?;
        let rate = match role {
            LiquidityRole::Maker => self.maker_rate_bps,
            LiquidityRole::Taker => self.taker_rate_bps,
        };
        let amount = fill.quantity * fill.price * rate / Decimal18::from_i64(BASIS_POINTS);
        Ok(vec![FeeComponent::new(
            &format!("{}-fee", role_name(role)),
            self.rounding.apply(amount),
        )?])
    }
}

/// A YAML-selected set of models covering every supported asset class.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeSchedule {
    /// Schedule identifier.
    pub schedule_id: String,
    /// ISO currency code.
    pub currency: String,
    /// Equity model.
    pub equity: EquityRegulatoryFeeModel,
    /// Option model.
    pub option: OptionsPerContractFeeModel,
    /// Crypto model.
    pub crypto: CryptoMakerTakerFeeModel,
}

impl FeeSchedule {
    /// Construct a validated schedule.
    pub fn new(
        schedule_id: &str,
        currency: &str,
        equity: EquityRegulatoryFeeModel,
        option: OptionsPerContractFeeModel,
        crypto: CryptoMakerTakerFeeModel,
    ) -> LiveResult<Self> {
        Ok(Self {
            schedule_id: identifier(schedule_id, "schedule_id")?,
            currency: currency_code(currency)?,
            equity,
            option,
            crypto,
        })
    }

    /// Select a model by explicit asset class and assess one fill.
    pub fn calculate(&self, fill: &Fill, context: &FeeContext) -> LiveResult<FeeAssessment> {
        let components = match context.asset_class {
            AssetClass::Equity => self.equity.calculate(fill)?,
            AssetClass::Option => self.option.calculate(fill)?,
            AssetClass::Crypto => self.crypto.calculate(fill)?,
        };
        FeeAssessment::new(
            &fill.fill_id,
            &self.schedule_id,
            &self.currency,
            context.clone(),
            components,
        )
    }

    /// Return a new fill carrying the configured fee and its audit detail.
    pub fn apply(&self, fill: &Fill, context: &FeeContext) -> LiveResult<(Fill, FeeAssessment)> {
        let assessment = self.calculate(fill, context)?;
        let mut updated = fill.clone();
        updated.fee = assessment.total();
        Ok((updated, assessment))
    }
}

/// One exact currency total for an aggregation key.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeAggregate {
    /// Aggregation key.
    pub key: String,
    /// ISO currency code.
    pub currency: String,
    /// Exact total.
    pub amount: Decimal18,
}

/// Deterministic fee totals across all required attribution dimensions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeAggregation {
    /// Totals by run.
    pub by_run: Vec<FeeAggregate>,
    /// Totals by strategy.
    pub by_strategy: Vec<FeeAggregate>,
    /// Totals by asset class.
    pub by_asset_class: Vec<FeeAggregate>,
}

/// Aggregate assessments by run, strategy, and asset class and currency.
pub fn aggregate_fees(assessments: &[FeeAssessment]) -> LiveResult<FeeAggregation> {
    let mut by_run: BTreeMap<(String, String), Decimal18> = BTreeMap::new();
    let mut by_strategy: BTreeMap<(String, String), Decimal18> = BTreeMap::new();
    let mut by_asset_class: BTreeMap<(String, String), Decimal18> = BTreeMap::new();
    let mut seen: std::collections::HashSet<(String, String)> = std::collections::HashSet::new();
    for assessment in assessments {
        let fill_key = (assessment.run_id().to_string(), assessment.fill_id.clone());
        if !seen.insert(fill_key.clone()) {
            return Err(LiveError::Fee(format!(
                "duplicate fee assessment for run/fill {}/{}",
                fill_key.0, fill_key.1
            )));
        }
        add_total(&mut by_run, assessment.run_id(), assessment);
        add_total(&mut by_strategy, assessment.strategy_id(), assessment);
        add_total(
            &mut by_asset_class,
            assessment.asset_class().as_str(),
            assessment,
        );
    }
    Ok(FeeAggregation {
        by_run: aggregates(by_run),
        by_strategy: aggregates(by_strategy),
        by_asset_class: aggregates(by_asset_class),
    })
}

fn add_total(
    totals: &mut BTreeMap<(String, String), Decimal18>,
    key: &str,
    assessment: &FeeAssessment,
) {
    let entry = totals
        .entry((key.to_string(), assessment.currency.clone()))
        .or_insert(ZERO);
    *entry += assessment.total();
}

fn aggregates(totals: BTreeMap<(String, String), Decimal18>) -> Vec<FeeAggregate> {
    totals
        .into_iter()
        .map(|((key, currency), amount)| FeeAggregate {
            key,
            currency,
            amount,
        })
        .collect()
}

fn role_name(role: LiquidityRole) -> &'static str {
    match role {
        LiquidityRole::Maker => "maker",
        LiquidityRole::Taker => "taker",
    }
}

fn is_integral(value: Decimal18) -> bool {
    value
        .round_to(RoundingMode::Truncate, 0)
        .map(|rounded| rounded == value)
        .unwrap_or(false)
}

fn power_of_ten_places(precision: Decimal18) -> Option<u32> {
    if !precision.is_positive() {
        return None;
    }
    let mut value = precision.unscaled().unsigned_abs();
    let mut places: i64 = 18;
    while value > 1 && value.is_multiple_of(10) {
        value /= 10;
        places -= 1;
    }
    if value != 1 || !(0..=18).contains(&places) {
        return None;
    }
    Some(places as u32)
}

fn identifier(value: &str, field: &str) -> LiveResult<String> {
    let text = require_text(value, field)?;
    let valid = text
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric())
        && text
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'));
    if !valid {
        return Err(LiveError::Fee(format!(
            "{field} contains invalid characters"
        )));
    }
    Ok(text)
}

fn currency_code(value: &str) -> LiveResult<String> {
    let bytes = value.as_bytes();
    if bytes.len() != 3 || !bytes.iter().all(u8::is_ascii_uppercase) {
        return Err(LiveError::Fee(
            "currency must be a three-letter uppercase code".into(),
        ));
    }
    Ok(value.to_string())
}

#[cfg(test)]
#[path = "../../tests/unit/fills_fees.rs"]
mod tests;
