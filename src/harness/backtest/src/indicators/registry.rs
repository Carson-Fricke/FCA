//! Typed indicator registry mapping declarative names to factories.
//!
//! Port of `harness.indicators.registry`. Consumers request indicators by name
//! and parameter mapping and receive a fresh, symbol-bound [`TickIndicator`].
//! Every parameter is validated against the spec's typed schema before
//! construction, so invalid requests fail loudly instead of silently changing
//! indicator semantics. The shared `bucket` emission-interval parameter is
//! appended to every spec automatically.

use std::collections::BTreeMap;

use super::base::{
    IndicatorError, IndicatorMachine, TickIndicator, DEFAULT_BUCKET_NS, MIDNIGHT_UTC_NS,
};
use super::momentum::RsiKernel;
use super::overlays::{EmaKernel, MidSmaKernel, SmaKernel};
use super::volatility::{AtrKernel, RealizedVolKernel};
use super::volume::{DollarVolumeKernel, ObvKernel, VwapKernel};

/// Typed parameter vocabulary supported by indicator specs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IndicatorParamKind {
    /// A positive integer.
    Integer,
    /// A duration string or nanosecond count.
    Duration,
}

/// One typed indicator parameter declaration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IndicatorParam {
    /// Parameter name.
    pub name: String,
    /// Parameter kind.
    pub kind: IndicatorParamKind,
    /// Whether the parameter must be supplied.
    pub required: bool,
    /// Optional integer default.
    pub default_int: Option<i64>,
    /// Optional duration default.
    pub default_duration: Option<String>,
}

impl IndicatorParam {
    /// Declare a required integer parameter.
    #[must_use]
    pub fn integer(name: &str) -> Self {
        Self {
            name: name.to_string(),
            kind: IndicatorParamKind::Integer,
            required: true,
            default_int: None,
            default_duration: None,
        }
    }

    /// Declare a required duration parameter.
    #[must_use]
    pub fn required_duration(name: &str) -> Self {
        Self {
            name: name.to_string(),
            kind: IndicatorParamKind::Duration,
            required: true,
            default_int: None,
            default_duration: None,
        }
    }

    /// Declare an optional duration parameter with a default.
    #[must_use]
    pub fn duration(name: &str, default: &str) -> Self {
        Self {
            name: name.to_string(),
            kind: IndicatorParamKind::Duration,
            required: false,
            default_int: None,
            default_duration: Some(default.to_string()),
        }
    }

    /// Declare an optional duration parameter with no default.
    #[must_use]
    pub fn optional_duration(name: &str) -> Self {
        Self {
            name: name.to_string(),
            kind: IndicatorParamKind::Duration,
            required: false,
            default_int: None,
            default_duration: None,
        }
    }
}

/// A requested parameter value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ParamValue {
    /// An integer request.
    Int(i64),
    /// A duration request.
    Duration(String),
}

/// The concrete built-in indicator behind one spec.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IndicatorKind {
    /// Simple moving average.
    Sma,
    /// Exponential moving average.
    Ema,
    /// Wilder relative strength index.
    Rsi,
    /// Mid-price simple moving average.
    MidSma,
    /// Wilder average true range.
    Atr,
    /// Rolling realized volatility of returns.
    RealizedVol,
    /// Session-cumulative volume-weighted average price.
    Vwap,
    /// Traded notional, cumulative or rolling.
    DollarVolume,
    /// On-balance volume.
    Obv,
}

/// One registered indicator: name, typed parameter schema, and kind.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IndicatorSpec {
    /// Registered name.
    pub name: String,
    /// Human description.
    pub description: String,
    /// Typed parameters, including the shared `bucket` parameter.
    pub params: Vec<IndicatorParam>,
    /// Concrete kind.
    pub kind: IndicatorKind,
}

/// A symbol-bound indicator built from a validated request.
pub struct ConfiguredIndicator {
    /// Instrument symbol.
    pub symbol: String,
    /// The constructed indicator.
    pub indicator: Box<dyn TickIndicator>,
}

/// A name-to-spec registry with typed parameter validation.
#[derive(Clone, Debug, Default)]
pub struct IndicatorRegistry {
    specs: BTreeMap<String, IndicatorSpec>,
}

impl IndicatorRegistry {
    /// Create an empty registry.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Register one spec, rejecting duplicate names.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Configuration`] for a duplicate name.
    pub fn register(&mut self, spec: IndicatorSpec) -> Result<(), IndicatorError> {
        if !is_valid_name(&spec.name) {
            return Err(IndicatorError::Configuration(
                "indicator name must match ^[a-z][a-z0-9_]*$".to_string(),
            ));
        }
        if self.specs.contains_key(&spec.name) {
            return Err(IndicatorError::Configuration(format!(
                "indicator {:?} is already registered",
                spec.name
            )));
        }
        self.specs.insert(spec.name.clone(), spec);
        Ok(())
    }

    /// Return the spec registered under `name`.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError::Unknown`] for an unknown name.
    pub fn spec(&self, name: &str) -> Result<&IndicatorSpec, IndicatorError> {
        self.specs
            .get(name.trim())
            .ok_or_else(|| IndicatorError::Unknown(format!("unknown indicator {:?}", name.trim())))
    }

    /// Return registered indicator names in lexical order.
    #[must_use]
    pub fn names(&self) -> Vec<String> {
        self.specs.keys().cloned().collect()
    }

    /// Build a fresh symbol-bound indicator from a validated request.
    ///
    /// # Errors
    ///
    /// Returns [`IndicatorError`] when the name is unknown, a required
    /// parameter is missing, an unknown parameter is supplied, or a value has
    /// the wrong type.
    pub fn create(
        &self,
        name: &str,
        params: &BTreeMap<String, ParamValue>,
        symbol: &str,
    ) -> Result<ConfiguredIndicator, IndicatorError> {
        let spec = self.spec(name)?;
        let resolved = resolve_parameters(spec, params)?;
        let bucket_ns = match resolved_duration(&resolved, "bucket")? {
            Some(value) => value,
            None => DEFAULT_BUCKET_NS,
        };
        let symbol = symbol.to_uppercase();
        let indicator: Box<dyn TickIndicator> = match spec.kind {
            IndicatorKind::Sma => machine(
                SmaKernel::new(resolved_integer(&resolved, "period")?)?,
                &symbol,
                bucket_ns,
            )?,
            IndicatorKind::Ema => machine(
                EmaKernel::new(resolved_integer(&resolved, "span")?)?,
                &symbol,
                bucket_ns,
            )?,
            IndicatorKind::Rsi => machine(
                RsiKernel::new(resolved_integer(&resolved, "period")?)?,
                &symbol,
                bucket_ns,
            )?,
            IndicatorKind::MidSma => machine(
                MidSmaKernel::new(resolved_integer(&resolved, "period")?)?,
                &symbol,
                bucket_ns,
            )?,
            IndicatorKind::Atr => machine(
                AtrKernel::new(resolved_integer(&resolved, "period")?)?,
                &symbol,
                bucket_ns,
            )?,
            IndicatorKind::RealizedVol => {
                let window = require_duration(&resolved, "window")?;
                machine(RealizedVolKernel::new(window)?, &symbol, bucket_ns)?
            }
            IndicatorKind::Vwap => machine(VwapKernel::new(), &symbol, bucket_ns)?,
            IndicatorKind::DollarVolume => {
                let window = resolved_duration(&resolved, "window")?;
                machine(DollarVolumeKernel::new(window)?, &symbol, bucket_ns)?
            }
            IndicatorKind::Obv => machine(ObvKernel::new(), &symbol, bucket_ns)?,
        };
        Ok(ConfiguredIndicator { symbol, indicator })
    }
}

fn machine<K: super::base::IndicatorKernel + 'static>(
    kernel: K,
    symbol: &str,
    bucket_ns: i64,
) -> Result<Box<dyn TickIndicator>, IndicatorError> {
    Ok(Box::new(IndicatorMachine::with_kernel(
        kernel,
        symbol,
        bucket_ns,
        Some(MIDNIGHT_UTC_NS),
    )?))
}

fn resolved_integer(
    resolved: &BTreeMap<String, ParamValue>,
    name: &str,
) -> Result<i64, IndicatorError> {
    match resolved.get(name) {
        Some(ParamValue::Int(value)) => Ok(*value),
        _ => Err(IndicatorError::Configuration(format!(
            "parameter {name:?} must be an integer"
        ))),
    }
}

fn resolved_duration(
    resolved: &BTreeMap<String, ParamValue>,
    name: &str,
) -> Result<Option<i64>, IndicatorError> {
    match resolved.get(name) {
        Some(ParamValue::Duration(text)) => parse_duration_ns(text).map(Some).map_err(|message| {
            IndicatorError::Configuration(format!("parameter {name:?} {message}"))
        }),
        Some(_) => Err(IndicatorError::Configuration(format!(
            "parameter {name:?} must be a duration string"
        ))),
        None => Ok(None),
    }
}

fn require_duration(
    resolved: &BTreeMap<String, ParamValue>,
    name: &str,
) -> Result<i64, IndicatorError> {
    resolved_duration(resolved, name)?
        .ok_or_else(|| IndicatorError::Configuration(format!("parameter {name:?} is required")))
}

fn is_valid_name(name: &str) -> bool {
    let mut chars = name.chars();
    match chars.next() {
        Some(first) if first.is_ascii_lowercase() => {}
        _ => return false,
    }
    chars.all(|character| {
        character.is_ascii_lowercase() || character.is_ascii_digit() || character == '_'
    })
}

fn resolve_parameters(
    spec: &IndicatorSpec,
    requested: &BTreeMap<String, ParamValue>,
) -> Result<BTreeMap<String, ParamValue>, IndicatorError> {
    let mut resolved = BTreeMap::new();
    for param in &spec.params {
        if let Some(value) = requested.get(&param.name) {
            resolved.insert(param.name.clone(), validate(param, value)?);
        } else if param.required {
            return Err(IndicatorError::Configuration(format!(
                "indicator {:?} requires parameter {:?}",
                spec.name, param.name
            )));
        } else if let Some(default) = &param.default_int {
            resolved.insert(param.name.clone(), ParamValue::Int(*default));
        } else if let Some(default) = &param.default_duration {
            resolved.insert(param.name.clone(), ParamValue::Duration(default.clone()));
        }
    }
    for name in requested.keys() {
        if !spec.params.iter().any(|param| param.name == *name) {
            return Err(IndicatorError::Configuration(format!(
                "indicator {:?} does not accept parameter {name:?}",
                spec.name
            )));
        }
    }
    Ok(resolved)
}

fn validate(param: &IndicatorParam, value: &ParamValue) -> Result<ParamValue, IndicatorError> {
    match (param.kind, value) {
        (IndicatorParamKind::Integer, ParamValue::Int(number)) => {
            if *number < 1 {
                return Err(IndicatorError::Configuration(format!(
                    "parameter {:?} must be positive",
                    param.name
                )));
            }
            Ok(ParamValue::Int(*number))
        }
        (IndicatorParamKind::Integer, _) => Err(IndicatorError::Configuration(format!(
            "parameter {:?} must be an integer",
            param.name
        ))),
        (IndicatorParamKind::Duration, ParamValue::Duration(text)) => {
            parse_duration_ns(text).map_err(|message| {
                IndicatorError::Configuration(format!("parameter {:?} {message}", param.name))
            })?;
            Ok(ParamValue::Duration(text.clone()))
        }
        (IndicatorParamKind::Duration, _) => Err(IndicatorError::Configuration(format!(
            "parameter {:?} must be a duration string",
            param.name
        ))),
    }
}

/// Parse a simple duration string (`ns`, `us`, `ms`, `s`, `min`, `h`, `d`).
///
/// # Errors
///
/// Returns an error message when the duration is malformed or non-positive.
pub fn parse_duration_ns(text: &str) -> Result<i64, String> {
    let text = text.trim();
    let split = text
        .find(|character: char| !character.is_ascii_digit())
        .ok_or_else(|| "must be a duration string".to_string())?;
    let (digits, unit) = text.split_at(split);
    let magnitude: i64 = digits.parse().map_err(|_| "must be positive".to_string())?;
    if magnitude <= 0 {
        return Err("must be positive".to_string());
    }
    let multiplier = match unit.trim() {
        "ns" | "" => 1,
        "us" => 1_000,
        "ms" => 1_000_000,
        "s" | "sec" | "secs" => 1_000_000_000,
        "m" | "min" | "mins" => 60 * 1_000_000_000,
        "h" | "hr" | "hour" | "hours" => 3_600 * 1_000_000_000,
        "d" | "day" | "days" => 86_400 * 1_000_000_000,
        other => return Err(format!("has unknown unit {other:?}")),
    };
    magnitude
        .checked_mul(multiplier)
        .ok_or_else(|| "is out of range".to_string())
}

fn builtin(
    name: &str,
    description: &str,
    kind: IndicatorKind,
    mut params: Vec<IndicatorParam>,
) -> IndicatorSpec {
    params.push(IndicatorParam::duration("bucket", "1min"));
    IndicatorSpec {
        name: name.to_string(),
        description: description.to_string(),
        params,
        kind,
    }
}

/// Build the registry of built-in indicators.
///
/// # Panics
///
/// Panics only if the static built-in specs violate the registry invariants.
#[must_use]
pub fn default_registry() -> IndicatorRegistry {
    let mut registry = IndicatorRegistry::new();
    let entries = [
        builtin(
            "sma",
            "Rolling simple mean of the last period trade prices.",
            IndicatorKind::Sma,
            vec![IndicatorParam::integer("period")],
        ),
        builtin(
            "ema",
            "Recursive exponential mean of trade prices seeded on the first trade.",
            IndicatorKind::Ema,
            vec![IndicatorParam::integer("span")],
        ),
        builtin(
            "rsi",
            "Wilder relative strength index over consecutive trade-price changes.",
            IndicatorKind::Rsi,
            vec![IndicatorParam::integer("period")],
        ),
        builtin(
            "atr",
            "Wilder average true range over per-bucket trade ranges.",
            IndicatorKind::Atr,
            vec![IndicatorParam::integer("period")],
        ),
        builtin(
            "realized_vol",
            "Rolling standard deviation of tick-to-tick simple returns.",
            IndicatorKind::RealizedVol,
            vec![IndicatorParam::required_duration("window")],
        ),
        builtin(
            "dollar_volume",
            "Traded notional; cumulative per session, or rolling over a window.",
            IndicatorKind::DollarVolume,
            vec![IndicatorParam::optional_duration("window")],
        ),
        builtin(
            "vwap",
            "Session-cumulative volume-weighted average price of trades.",
            IndicatorKind::Vwap,
            vec![],
        ),
        builtin(
            "obv",
            "On-balance volume from the tick rule, cumulative per session.",
            IndicatorKind::Obv,
            vec![],
        ),
        builtin(
            "mid_sma",
            "Rolling simple mean of the last period quote mid-prices.",
            IndicatorKind::MidSma,
            vec![IndicatorParam::integer("period")],
        ),
    ];
    for spec in entries {
        registry.register(spec).expect("built-in spec is valid");
    }
    registry
}

/// Build an indicator from the default registry by declarative name.
///
/// # Errors
///
/// Returns [`IndicatorError`] under the same conditions as
/// [`IndicatorRegistry::create`].
pub fn create_indicator(
    name: &str,
    params: &BTreeMap<String, ParamValue>,
    symbol: &str,
) -> Result<ConfiguredIndicator, IndicatorError> {
    default_registry().create(name, params, symbol)
}

/// Return every built-in indicator name in lexical order.
#[must_use]
pub fn indicator_names() -> Vec<String> {
    default_registry().names()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::indicators::base::{Tick, TickDriver, TickKind};
    use num::Decimal18;

    fn params(pairs: &[(&str, ParamValue)]) -> BTreeMap<String, ParamValue> {
        pairs
            .iter()
            .map(|(name, value)| ((*name).to_string(), value.clone()))
            .collect()
    }

    fn trade(sequence: u64, price: i64) -> Tick {
        Tick {
            timestamp_ns: sequence as i64,
            sequence,
            kind: TickKind::Trade {
                price: Decimal18::from_i64(price),
                quantity: Decimal18::from_i64(1),
            },
        }
    }

    #[test]
    fn default_registry_names_are_sorted() {
        assert_eq!(
            indicator_names(),
            vec![
                "atr",
                "dollar_volume",
                "ema",
                "mid_sma",
                "obv",
                "realized_vol",
                "rsi",
                "sma",
                "vwap"
            ]
            .into_iter()
            .map(str::to_string)
            .collect::<Vec<_>>()
        );
    }

    #[test]
    fn create_validates_and_builds() {
        let params = params(&[("period", ParamValue::Int(2))]);
        let configured = create_indicator("sma", &params, "spy").expect("create");
        assert_eq!(configured.symbol, "SPY");
        let mut indicator = configured.indicator;
        assert_eq!(indicator.name(), "sma");
        assert_eq!(indicator.drivers(), &[TickDriver::Trade]);
        for sequence in 0..2u64 {
            indicator.on_tick(&trade(sequence, 10)).expect("tick");
        }
        assert_eq!(indicator.value(), Some(Decimal18::from_i64(10)));
    }

    #[test]
    fn create_builds_volatility_and_volume_builtins() {
        let configured = create_indicator("atr", &params(&[("period", ParamValue::Int(2))]), "spy")
            .expect("atr");
        assert_eq!(configured.indicator.name(), "atr");
        assert_eq!(configured.symbol, "SPY");

        let configured = create_indicator(
            "realized_vol",
            &params(&[("window", ParamValue::Duration("5s".to_string()))]),
            "spy",
        )
        .expect("realized_vol");
        assert_eq!(configured.indicator.name(), "realized_vol");

        let configured = create_indicator(
            "dollar_volume",
            &params(&[("window", ParamValue::Duration("1min".to_string()))]),
            "spy",
        )
        .expect("dollar_volume");
        assert_eq!(configured.indicator.name(), "dollar_volume");

        let configured = create_indicator("vwap", &BTreeMap::new(), "spy").expect("vwap");
        assert_eq!(configured.indicator.name(), "vwap");

        let configured = create_indicator("obv", &BTreeMap::new(), "spy").expect("obv");
        assert_eq!(configured.indicator.name(), "obv");
    }

    #[test]
    fn realized_vol_requires_a_window() {
        assert!(create_indicator("realized_vol", &BTreeMap::new(), "spy").is_err());
    }

    #[test]
    fn rejects_unknown_and_missing_params() {
        assert!(create_indicator("nope", &BTreeMap::new(), "spy").is_err());
        assert!(create_indicator("sma", &BTreeMap::new(), "spy").is_err());
        let unknown = params(&[("nope", ParamValue::Int(1))]);
        assert!(create_indicator("sma", &unknown, "spy").is_err());
    }

    #[test]
    fn duration_parser_handles_common_units() {
        assert_eq!(parse_duration_ns("1min").expect("min"), 60_000_000_000);
        assert_eq!(parse_duration_ns("5s").expect("s"), 5_000_000_000);
        assert_eq!(parse_duration_ns("250ms").expect("ms"), 250_000_000);
        assert!(parse_duration_ns("0s").is_err());
        assert!(parse_duration_ns("hello").is_err());
    }
}
