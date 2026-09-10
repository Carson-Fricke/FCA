//! Pessimistic fill plugin selection for the backtest runner.
//!
//! Port of `harness.runtime.backtest.fills`. The actual fill simulations live in
//! `harness.fills` and belong to another epic; this module ports the exact,
//! deterministic selection surface: [`FillModelOptions`], [`build_fill_model`],
//! the JSON-ready identity used by the reproducibility manifest, and the
//! declared response latency.

use crate::canonical_json::JsonValue;

use super::errors::FillModel;

/// Whether a partially filled resting order's remainder rests or is canceled.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RemainderPolicy {
    /// Keep the remainder resting.
    #[default]
    Rest,
    /// Cancel the remainder after a partial fill.
    Cancel,
}

impl RemainderPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Rest => "rest",
            Self::Cancel => "cancel",
        }
    }

    /// Parse a remainder policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "rest" => Some(Self::Rest),
            "cancel" => Some(Self::Cancel),
            _ => None,
        }
    }
}

/// Exact, deterministic options shared by the pessimistic fill plugins.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillModelOptions {
    /// Deterministic response delay in nanoseconds.
    pub response_latency_ns: i64,
    /// Partial-fill remainder handling.
    pub remainder_policy: RemainderPolicy,
    /// Whether a resting limit requires a trade-through print.
    pub require_trade_through: bool,
    /// String-exact minimum trade-through distance.
    pub minimum_trade_through: String,
    /// Whether the trade-through model is strict.
    pub strict: bool,
}

impl Default for FillModelOptions {
    fn default() -> Self {
        Self {
            response_latency_ns: 0,
            remainder_policy: RemainderPolicy::Rest,
            require_trade_through: true,
            minimum_trade_through: "0".to_string(),
            strict: true,
        }
    }
}

/// One selected fill plugin.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FillPlugin {
    /// Fill at the first visible touch.
    SimpleTouch,
    /// Require a qualifying print for a resting limit.
    LatencySize {
        /// Declared response delay in nanoseconds.
        response_latency_ns: i64,
        /// Partial-fill remainder handling.
        remainder_policy: RemainderPolicy,
        /// Whether a trade-through is required.
        require_trade_through: bool,
    },
    /// Strictest variant; never fills from a quote alone.
    TradeThrough {
        /// Declared response delay in nanoseconds.
        response_latency_ns: i64,
        /// String-exact minimum trade-through distance.
        minimum_trade_through: String,
        /// Whether the model is strict.
        strict: bool,
        /// Partial-fill remainder handling.
        remainder_policy: RemainderPolicy,
    },
}

/// Select one deterministic fill plugin by name.
#[must_use]
pub fn build_fill_model(model: FillModel, options: Option<&FillModelOptions>) -> FillPlugin {
    let chosen = options.cloned().unwrap_or_default();
    match model {
        FillModel::SimpleTouch => FillPlugin::SimpleTouch,
        FillModel::LatencySize => FillPlugin::LatencySize {
            response_latency_ns: chosen.response_latency_ns,
            remainder_policy: chosen.remainder_policy,
            require_trade_through: chosen.require_trade_through,
        },
        FillModel::TradeThrough => FillPlugin::TradeThrough {
            response_latency_ns: chosen.response_latency_ns,
            minimum_trade_through: chosen.minimum_trade_through,
            strict: chosen.strict,
            remainder_policy: chosen.remainder_policy,
        },
    }
}

/// Return the declared deterministic response delay in microseconds.
#[must_use]
pub fn fill_model_latency_us(plugin: &FillPlugin) -> i64 {
    match plugin {
        FillPlugin::SimpleTouch => 0,
        FillPlugin::LatencySize {
            response_latency_ns,
            ..
        }
        | FillPlugin::TradeThrough {
            response_latency_ns,
            ..
        } => response_latency_ns.div_euclid(super::errors::MICROSECOND_NS),
    }
}

/// Return a canonical, JSON-ready description of one fill plugin.
#[must_use]
pub fn fill_model_identity(plugin: &FillPlugin) -> JsonValue {
    match plugin {
        FillPlugin::SimpleTouch => JsonValue::object([(
            "model",
            JsonValue::string(FillModel::SimpleTouch.value()),
        )]),
        FillPlugin::LatencySize {
            response_latency_ns,
            remainder_policy,
            require_trade_through,
        } => JsonValue::object([
            ("model", JsonValue::string(FillModel::LatencySize.value())),
            (
                "response_latency_us",
                JsonValue::Int(response_latency_ns.div_euclid(super::errors::MICROSECOND_NS)),
            ),
            ("remainder_policy", JsonValue::string(remainder_policy.value())),
            (
                "require_trade_through",
                JsonValue::Bool(*require_trade_through),
            ),
        ]),
        FillPlugin::TradeThrough {
            response_latency_ns,
            minimum_trade_through,
            strict,
            remainder_policy,
        } => JsonValue::object([
            (
                "model",
                JsonValue::string(FillModel::TradeThrough.value()),
            ),
            (
                "response_latency_us",
                JsonValue::Int(response_latency_ns.div_euclid(super::errors::MICROSECOND_NS)),
            ),
            (
                "minimum_trade_through",
                JsonValue::String(minimum_trade_through.clone()),
            ),
            ("strict", JsonValue::Bool(*strict)),
            ("remainder_policy", JsonValue::string(remainder_policy.value())),
        ]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple_touch_identity() {
        let plugin = build_fill_model(FillModel::SimpleTouch, None);
        assert_eq!(
            fill_model_identity(&plugin).to_canonical_string(),
            r#"{"model":"simple_touch"}"#
        );
        assert_eq!(fill_model_latency_us(&plugin), 0);
    }

    #[test]
    fn latency_size_identity_carries_options() {
        let options = FillModelOptions {
            response_latency_ns: 5_000,
            remainder_policy: RemainderPolicy::Cancel,
            require_trade_through: false,
            ..FillModelOptions::default()
        };
        let plugin = build_fill_model(FillModel::LatencySize, Some(&options));
        assert_eq!(
            fill_model_identity(&plugin).to_canonical_string(),
            r#"{"model":"latency_size","remainder_policy":"cancel","require_trade_through":false,"response_latency_us":5}"#
        );
        assert_eq!(fill_model_latency_us(&plugin), 5);
    }

    #[test]
    fn trade_through_identity_includes_minimum() {
        let options = FillModelOptions {
            minimum_trade_through: "0.01".to_string(),
            ..FillModelOptions::default()
        };
        let plugin = build_fill_model(FillModel::TradeThrough, Some(&options));
        assert_eq!(
            fill_model_identity(&plugin).to_canonical_string(),
            r#"{"minimum_trade_through":"0.01","model":"trade_through","remainder_policy":"rest","response_latency_us":0,"strict":true}"#
        );
    }

    #[test]
    fn remainder_policy_round_trips() {
        for policy in [RemainderPolicy::Rest, RemainderPolicy::Cancel] {
            assert_eq!(RemainderPolicy::parse(policy.value()), Some(policy));
        }
    }
}
