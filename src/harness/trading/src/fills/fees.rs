//! Exact, configuration-driven fee calculation and aggregation.

use std::collections::BTreeMap;

use num::Decimal18;
use num::RoundingMode;

use crate::accounting::Fill;
use crate::error::require_text;
use crate::error::LiveError;
use crate::error::LiveResult;
use crate::orders::Side;

const ZERO: Decimal18 = Decimal18::ZERO;

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
    precision: Decimal18,
    /// The rounding mode.
    mode: FeeRoundingMode,
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

    pub const fn precision(&self) -> Decimal18 {
        self.precision
    }
    pub const fn mode(&self) -> FeeRoundingMode {
        self.mode
    }

    /// Round one exact amount without introducing binary floating point.
    pub fn apply(&self, amount: Decimal18) -> LiveResult<Decimal18> {
        amount
            .round_to(self.mode.to_rounding(), self.places)
            .map_err(|e| LiveError::Fee(format!("fee rounding failed: {e}")))
    }
}

/// Engine-owned attribution supplied independently of strategy logic.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeContext {
    /// Execution job identifier.
    job_id: String,
    /// Strategy-instance identifier.
    strategy_id: String,
    /// The asset class selecting the fee model.
    asset_class: AssetClass,
}

impl FeeContext {
    /// Construct a validated context.
    pub fn new(job_id: &str, strategy_id: &str, asset_class: AssetClass) -> LiveResult<Self> {
        Ok(Self {
            job_id: require_text(job_id, "job_id")?,
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
    components: Vec<FeeComponent>,
    total: Decimal18,
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
        let total = components
            .iter()
            .try_fold(Decimal18::ZERO, |total, component| {
                total.checked_add(component.amount)
            })
            .map_err(|e| LiveError::Fee(format!("fee total cannot be represented: {e}")))?;
        Ok(Self {
            fill_id: require_text(fill_id, "fill_id")?,
            schedule_id: identifier(schedule_id, "schedule_id")?,
            currency: currency_code(currency)?,
            context,
            components,
            total,
        })
    }

    /// The exact sum of already-rounded components.
    pub fn total(&self) -> Decimal18 {
        self.total
    }

    pub fn components(&self) -> &[FeeComponent] {
        &self.components
    }

    /// The job attribution used by aggregation.
    pub fn job_id(&self) -> &str {
        &self.context.job_id
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
    rounding: FeeRounding,
    /// Per-share commission.
    commission_per_share: Decimal18,
    /// Minimum commission.
    commission_minimum: Decimal18,
    /// SEC rate in basis points.
    sec_rate_bps: Decimal18,
    /// FINRA TAF per share.
    taf_per_share: Decimal18,
    /// FINRA TAF cap.
    taf_maximum: Decimal18,
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
        components(
            crate::fee_policy::equity(
                fill.quantity,
                fill.price,
                fill.side == Side::Sell,
                crate::fee_policy::EquityRates {
                    commission_per_share: self.commission_per_share,
                    commission_minimum: self.commission_minimum,
                    sec_rate_bps: self.sec_rate_bps,
                    taf_per_share: self.taf_per_share,
                    taf_maximum: self.taf_maximum,
                },
                Decimal18::from_i64(10_000),
            ),
            self.rounding,
        )
    }
}

/// Commission and exchange/regulatory charges assessed per contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OptionsPerContractFeeModel {
    /// Rounding rule.
    rounding: FeeRounding,
    /// Per-contract commission.
    commission_per_contract: Decimal18,
    /// Per-contract regulatory fee.
    fee_per_contract: Decimal18,
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
        components(
            crate::fee_policy::options(
                fill.quantity,
                self.commission_per_contract,
                self.fee_per_contract,
            ),
            self.rounding,
        )
    }
}

/// Notional-basis-point fees selected from the fill's liquidity role.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CryptoMakerTakerFeeModel {
    /// Rounding rule.
    rounding: FeeRounding,
    /// Maker rate in basis points (may be negative for a rebate).
    maker_rate_bps: Decimal18,
    /// Taker rate in basis points.
    taker_rate_bps: Decimal18,
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
        let role = LiquidityRole::parse(fill.liquidity.as_deref().ok_or_else(|| {
            LiveError::Fee("crypto fills require maker or taker liquidity".into())
        })?)?;
        let maker = role == LiquidityRole::Maker;
        components(
            crate::fee_policy::crypto(
                fill.quantity,
                fill.price,
                if maker {
                    self.maker_rate_bps
                } else {
                    self.taker_rate_bps
                },
                maker,
                Decimal18::from_i64(10_000),
            ),
            self.rounding,
        )
    }
}

/// A YAML-selected set of models covering every supported asset class.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FeeSchedule {
    /// Schedule identifier.
    schedule_id: String,
    /// ISO currency code.
    currency: String,
    /// Equity model.
    equity: EquityRegulatoryFeeModel,
    /// Option model.
    option: OptionsPerContractFeeModel,
    /// Crypto model.
    crypto: CryptoMakerTakerFeeModel,
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
    /// Totals by execution job.
    pub by_job: Vec<FeeAggregate>,
    /// Totals by strategy.
    pub by_strategy: Vec<FeeAggregate>,
    /// Totals by asset class.
    pub by_asset_class: Vec<FeeAggregate>,
}

/// Aggregate assessments by job, strategy, and asset class and currency.
pub fn aggregate_fees(assessments: &[FeeAssessment]) -> LiveResult<FeeAggregation> {
    let mut by_job: BTreeMap<(String, String), Decimal18> = BTreeMap::new();
    let mut by_strategy: BTreeMap<(String, String), Decimal18> = BTreeMap::new();
    let mut by_asset_class: BTreeMap<(String, String), Decimal18> = BTreeMap::new();
    let mut seen: std::collections::HashSet<(String, String)> = std::collections::HashSet::new();
    for assessment in assessments {
        let fill_key = (assessment.job_id().to_string(), assessment.fill_id.clone());
        if !seen.insert(fill_key.clone()) {
            return Err(LiveError::Fee(format!(
                "duplicate fee assessment for job/fill {}/{}",
                fill_key.0, fill_key.1
            )));
        }
        add_total(&mut by_job, assessment.job_id(), assessment)?;
        add_total(&mut by_strategy, assessment.strategy_id(), assessment)?;
        add_total(
            &mut by_asset_class,
            assessment.asset_class().as_str(),
            assessment,
        )?;
    }
    Ok(FeeAggregation {
        by_job: aggregates(by_job),
        by_strategy: aggregates(by_strategy),
        by_asset_class: aggregates(by_asset_class),
    })
}

fn add_total(
    totals: &mut BTreeMap<(String, String), Decimal18>,
    key: &str,
    assessment: &FeeAssessment,
) -> LiveResult<()> {
    let entry = totals
        .entry((key.to_string(), assessment.currency.clone()))
        .or_insert(ZERO);
    *entry = entry
        .checked_add(assessment.total())
        .map_err(|e| LiveError::Fee(format!("fee aggregate overflow: {e}")))?;
    Ok(())
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

fn components(
    raw: Result<Vec<(&str, Decimal18)>, crate::fee_policy::FeeError>,
    rounding: FeeRounding,
) -> LiveResult<Vec<FeeComponent>> {
    raw.map_err(|e| LiveError::Fee(format!("fee arithmetic failed: {e}")))?
        .into_iter()
        .map(|(name, value)| FeeComponent::new(name, rounding.apply(value)?))
        .collect()
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
