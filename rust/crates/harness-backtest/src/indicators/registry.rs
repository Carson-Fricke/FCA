//! Typed indicator registry mapping declarative names to factories.
//!
//! Port of `harness.indicators.registry`. Consumers request indicators by name
//! and parameter mapping and receive a fresh, symbol-bound [`TickIndicator`].
//! Every parameter is validated against the spec's typed schema before
//! construction, so invalid requests fail loudly instead of silently changing
//! indicator semantics. The shared `bucket` emission-interval parameter is
//! appended to every spec automatically.

use std::collections::BTreeMap;

use super::base::{IndicatorError, TickIndicator};
use super::momentum::RelativeStrengthIndex;
use super::overlays::{ExponentialMovingAverage, MidSimpleMovingAverage, SimpleMovingAverage};

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
        let indicator: Box<dyn TickIndicator> = match spec.kind {
            IndicatorKind::Sma => Box::new(SimpleMovingAverage::new(resolved_integer(
                &resolved, "period",
            )?)?),
            IndicatorKind::Ema => Box::new(ExponentialMovingAverage::new(resolved_integer(
                &resolved, "span",
            )?)?),
            IndicatorKind::Rsi => Box::new(RelativeStrengthIndex::new(resolved_integer(
                &resolved, "period",
            )?)?),
            IndicatorKind::MidSma => Box::new(MidSimpleMovingAverage::new(resolved_integer(
                &resolved, "period",
            )?)?),
        };
        Ok(ConfiguredIndicator {
            symbol: symbol.to_uppercase(),
            indicator,
        })
    }
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
    use harness_num::Decimal18;

    #[test]
    fn default_registry_names_are_sorted() {
        assert_eq!(
            indicator_names(),
            vec!["ema", "mid_sma", "rsi", "sma"]
                .into_iter()
                .map(str::to_string)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn create_validates_and_builds() {
        let params = BTreeMap::from([("period".to_string(), ParamValue::Int(2))]);
        let configured = create_indicator("sma", &params, "spy").expect("create");
        assert_eq!(configured.symbol, "SPY");
        let mut indicator = configured.indicator;
        assert_eq!(indicator.name(), "sma");
        assert_eq!(indicator.drivers(), &[TickDriver::Trade]);
        for sequence in 0..2u64 {
            indicator
                .on_tick(&Tick {
                    timestamp_ns: sequence as i64,
                    sequence,
                    kind: TickKind::Trade {
                        price: Decimal18::from_i64(10),
                        quantity: Decimal18::from_i64(1),
                    },
                })
                .expect("tick");
        }
        assert_eq!(indicator.value(), Some(Decimal18::from_i64(10)));
    }

    #[test]
    fn rejects_unknown_and_missing_params() {
        assert!(create_indicator("nope", &BTreeMap::new(), "spy").is_err());
        assert!(create_indicator("sma", &BTreeMap::new(), "spy").is_err());
        let unknown = BTreeMap::from([("nope".to_string(), ParamValue::Int(1))]);
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
