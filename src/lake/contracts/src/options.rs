//! Individual option selectors shared by the lake and research clients.

use serde::{Deserialize, Serialize};

use crate::{canonical_symbol, DataError, Date, DateTime, Decimal18};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OptionRight {
    Call,
    Put,
}

/// Resolve once at `as_of`, then retain the selected contract for the window.
/// Delta is an absolute target; the right determines its sign.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OptionSelector {
    pub underlying: String,
    pub right: OptionRight,
    pub as_of: String,
    #[serde(default)]
    pub expiration: Option<String>,
    #[serde(default)]
    pub days_to_expiration: Option<u32>,
    #[serde(default)]
    pub strike: Option<String>,
    #[serde(default)]
    pub delta: Option<String>,
    #[serde(default = "default_dte_tolerance")]
    pub dte_tolerance: u32,
    #[serde(default = "default_delta_tolerance")]
    pub delta_tolerance: String,
    #[serde(default = "default_quote_age")]
    pub max_quote_age_seconds: u32,
    /// Continuously compounded annual rate used by the delta estimator.
    #[serde(default = "zero")]
    pub risk_free_rate: String,
    /// Continuous annual dividend yield used by the delta estimator.
    #[serde(default = "zero")]
    pub dividend_yield: String,
}

fn default_dte_tolerance() -> u32 {
    7
}
fn default_delta_tolerance() -> String {
    "0.10".into()
}
fn default_quote_age() -> u32 {
    3600
}
fn zero() -> String {
    "0".into()
}

impl OptionSelector {
    pub fn validate(&self) -> Result<DateTime, DataError> {
        if canonical_symbol(&self.underlying)? != self.underlying {
            return Err(DataError::value(
                "option underlying must be canonical uppercase",
            ));
        }
        // Require a canonical UTC clock: the general lake parser also accepts
        // naive timestamps and normalizes overflowing clock components.
        let valid_clock = self
            .as_of
            .strip_suffix('Z')
            .and_then(|s| s.split_once('T'))
            .is_some_and(|(date, clock)| {
                let (clock, fraction) = clock.split_once('.').unwrap_or((clock, ""));
                let parts: Vec<_> = clock.split(':').collect();
                date.len() == 10
                    && parts.len() == 3
                    && parts
                        .iter()
                        .all(|p| p.len() == 2 && p.bytes().all(|b| b.is_ascii_digit()))
                    && parts[0].parse::<u32>().is_ok_and(|h| h < 24)
                    && parts[1].parse::<u32>().is_ok_and(|m| m < 60)
                    && parts[2].parse::<u32>().is_ok_and(|s| s < 60)
                    && fraction.len() <= 6
                    && fraction.bytes().all(|b| b.is_ascii_digit())
            });
        if !valid_clock {
            return Err(DataError::value(
                "option as_of must be an ISO UTC timestamp ending in Z",
            ));
        }
        let as_of = DateTime::parse_iso_utc(&self.as_of)
            .ok_or_else(|| DataError::value("option as_of must be a UTC timestamp"))?;
        if self.expiration.is_some() == self.days_to_expiration.is_some()
            || self.strike.is_some() == self.delta.is_some()
        {
            return Err(DataError::value(
                "choose expiration or days_to_expiration, and strike or delta",
            ));
        }
        if let Some(expiration) = &self.expiration {
            let date = Date::parse(expiration)
                .ok_or_else(|| DataError::value("invalid option expiration"))?;
            if date <= as_of.date() {
                return Err(DataError::value("option expiration must follow as_of"));
            }
        }
        if self.days_to_expiration == Some(0)
            || self.days_to_expiration.is_some_and(|d| d > 3650)
            || self.dte_tolerance > 366
            || self.max_quote_age_seconds == 0
            || self.max_quote_age_seconds > 86400
        {
            return Err(DataError::value(
                "invalid option DTE, tolerance, or quote age",
            ));
        }
        if let Some(strike) = &self.strike {
            let strike = strike
                .parse::<Decimal18>()
                .map_err(|e| DataError::value(e.to_string()))?;
            if !strike.is_positive() {
                return Err(DataError::value("option strike must be positive"));
            }
        }
        for (name, value, min, max) in [
            ("delta", self.delta.as_deref().unwrap_or("0.5"), 0.0, 1.0),
            ("delta_tolerance", self.delta_tolerance.as_str(), 0.0, 1.0),
            ("risk_free_rate", self.risk_free_rate.as_str(), -1.0, 1.0),
            ("dividend_yield", self.dividend_yield.as_str(), 0.0, 1.0),
        ] {
            let number: f64 = value
                .parse()
                .map_err(|_| DataError::value(format!("invalid option {name}")))?;
            if !number.is_finite()
                || number < min
                || number > max
                || (name == "delta" && (number == 0.0 || number == 1.0))
            {
                return Err(DataError::value(format!("invalid option {name}")));
            }
        }
        Ok(as_of)
    }
}

/// Selection evidence retained with the acquired input fingerprint.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolvedOption {
    pub symbol: String,
    pub underlying: String,
    pub expiration: String,
    pub strike: String,
    pub right: OptionRight,
    pub multiplier: u32,
    pub as_of: String,
    pub delta: Option<String>,
    pub quote_time: Option<String>,
    pub method: String,
}

impl ResolvedOption {
    /// Validate an untrusted resolver acknowledgement before acquiring data.
    pub fn validate_against(&self, selector: &OptionSelector) -> Result<(), DataError> {
        let as_of = selector.validate()?;
        let expiration = Date::parse(&self.expiration)
            .ok_or_else(|| DataError::Protocol("invalid resolved expiration".into()))?;
        let strike = self
            .strike
            .parse::<Decimal18>()
            .map_err(|e| DataError::Protocol(e.to_string()))?;
        let symbol = crate::canonical_osi_symbol(&self.symbol)?;
        let tail = &symbol[symbol.len() - 15..];
        let (year, month, day) = expiration.ymd();
        let date = format!("{:02}{month:02}{day:02}", year % 100);
        let right = match self.right {
            OptionRight::Call => 'C',
            OptionRight::Put => 'P',
        };
        let encoded_strike = tail[7..]
            .parse::<u32>()
            .map_err(|e| DataError::Protocol(e.to_string()))?;
        let expected_strike = format!("{}.{:03}", encoded_strike / 1000, encoded_strike % 1000)
            .parse::<Decimal18>()
            .expect("OSI decimal");
        if self.underlying != selector.underlying
            || self.right != selector.right
            || self.as_of != selector.as_of
            || self.multiplier != 100
            || symbol[..symbol.len() - 15] != selector.underlying
            || self.method.is_empty()
            || tail[..6] != date
            || tail.as_bytes()[6] != right as u8
            || strike != expected_strike
        {
            return Err(DataError::Protocol(
                "resolved contract identity does not match selector".into(),
            ));
        }
        if let Some(date) = &selector.expiration {
            if date != &self.expiration {
                return Err(DataError::Protocol("resolved expiration mismatch".into()));
            }
        } else {
            let target = as_of
                .date()
                .add_days(selector.days_to_expiration.unwrap() as i32);
            if target.days_until(expiration).unsigned_abs() > selector.dte_tolerance {
                return Err(DataError::Protocol(
                    "resolved expiration outside tolerance".into(),
                ));
            }
        }
        if let Some(expected) = &selector.strike {
            if expected.parse::<Decimal18>().expect("validated strike") != strike {
                return Err(DataError::Protocol("resolved strike mismatch".into()));
            }
        }
        let quote_time = self
            .quote_time
            .as_deref()
            .and_then(DateTime::parse_iso_utc)
            .ok_or_else(|| DataError::Protocol("resolved contract has no quote evidence".into()))?;
        if quote_time > as_of
            || as_of.micros() - quote_time.micros()
                > i64::from(selector.max_quote_age_seconds) * 1_000_000
        {
            return Err(DataError::Protocol(
                "resolved quote outside causal window".into(),
            ));
        }
        if let Some(target) = &selector.delta {
            let delta = self
                .delta
                .as_deref()
                .and_then(|d| d.parse::<f64>().ok())
                .ok_or_else(|| DataError::Protocol("resolved contract has no delta".into()))?;
            if !delta.is_finite()
                || delta.abs() > 1.0
                || (delta.abs() - target.parse::<f64>().unwrap()).abs()
                    > selector.delta_tolerance.parse::<f64>().unwrap()
                || (self.right == OptionRight::Call && delta <= 0.0)
                || (self.right == OptionRight::Put && delta >= 0.0)
            {
                return Err(DataError::Protocol(
                    "resolved delta outside target tolerance".into(),
                ));
            }
        }
        Ok(())
    }
}
