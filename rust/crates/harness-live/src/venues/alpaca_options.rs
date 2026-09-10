//! Alpaca options capability matrix and rejection taxonomy.
//!
//! Unsupported features are rejected before broker submission with an explicit
//! reason; nothing degrades silently.

use harness_num::{Decimal18, RoundingMode};

use crate::error::{LiveError, LiveResult};
use crate::orders::{MultiLegOrder, OrderType, TimeInForce};
use crate::symbol::is_osi_symbol;
use crate::symbol::Symbol;

/// Explicit rejection taxonomy for unsupported Alpaca options features.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum UnsupportedFeatureReason {
    /// Extended-hours flag.
    ExtendedHours,
    /// Bracket/OTO/OCO order class.
    OrderClass,
    /// Unsupported order type.
    OrderType,
    /// Unsupported time in force.
    TimeInForce,
    /// Leg-count violation.
    LegCount,
    /// Fractional ratio.
    LegRatio,
    /// Leg-shape violation.
    LegStructure,
    /// Non-integral quantity.
    Quantity,
    /// Order modification.
    OrderModification,
    /// Non-option asset class.
    AssetClass,
    /// Unknown feature key.
    UnknownFeature,
}

impl UnsupportedFeatureReason {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ExtendedHours => "extended_hours",
            Self::OrderClass => "order_class",
            Self::OrderType => "order_type",
            Self::TimeInForce => "time_in_force",
            Self::LegCount => "leg_count",
            Self::LegRatio => "leg_ratio",
            Self::LegStructure => "leg_structure",
            Self::Quantity => "quantity",
            Self::OrderModification => "order_modification",
            Self::AssetClass => "asset_class",
            Self::UnknownFeature => "unknown_feature",
        }
    }
}

/// One explicit unsupported-capability decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilityRejection {
    /// The rejection category.
    pub reason: UnsupportedFeatureReason,
    /// The offending feature.
    pub feature: String,
    /// A human-readable detail.
    pub detail: String,
}

/// Whether a capability is supported.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum VenueCapabilitySupport {
    /// Supported.
    Supported,
    /// Unsupported.
    Unsupported,
}

/// One advertised capability.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VenueCapability {
    /// The feature name.
    pub feature: String,
    /// Support state.
    pub support: VenueCapabilitySupport,
}

/// The versioned Alpaca options capability matrix.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaOptionsCapabilityMatrix {
    /// Maximum number of legs accepted.
    pub max_legs: usize,
    /// Supported time-in-force values.
    pub supported_time_in_force: Vec<TimeInForce>,
}

impl AlpacaOptionsCapabilityMatrix {
    /// The default Alpaca options matrix (multi-leg, day/GTC, no stops/IOC).
    pub fn alpaca_options_default() -> Self {
        Self {
            max_legs: 4,
            supported_time_in_force: vec![TimeInForce::Day, TimeInForce::Gtc],
        }
    }

    /// Explicit rejections for one multi-leg order.
    pub fn check(&self, order: &MultiLegOrder) -> Vec<CapabilityRejection> {
        let mut rejections = Vec::new();
        if matches!(order.order_type, OrderType::Stop | OrderType::StopLimit) {
            rejections.push(rejection(
                UnsupportedFeatureReason::OrderType,
                order.order_type.as_str(),
                "stop orders are not supported for multi-leg options",
            ));
        }
        if !self.supported_time_in_force.contains(&order.time_in_force) {
            rejections.push(rejection(
                UnsupportedFeatureReason::TimeInForce,
                order.time_in_force.as_str(),
                "time in force is not supported for multi-leg options",
            ));
        }
        if order.legs.len() < 2 || order.legs.len() > self.max_legs {
            rejections.push(rejection(
                UnsupportedFeatureReason::LegCount,
                "legs",
                "leg count is outside the supported range",
            ));
        }
        for leg in &order.legs {
            if !is_integral(leg.ratio) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::LegRatio,
                    "ratio",
                    "leg ratios must be positive integers",
                ));
            } else if !is_integral(leg.quantity / leg.ratio) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::Quantity,
                    "quantity",
                    "spread count must be a positive integer",
                ));
            }
            if !is_osi_symbol(leg.symbol.as_str()) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::AssetClass,
                    leg.symbol.as_str(),
                    "every leg must be an OSI option contract",
                ));
            }
        }
        rejections
    }

    /// Reject the order if any capability check fails.
    pub fn require(&self, order: &MultiLegOrder) -> LiveResult<()> {
        let rejections = self.check(order);
        if rejections.is_empty() {
            return Ok(());
        }
        let detail = rejections
            .iter()
            .map(|rejection| rejection.reason.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        Err(LiveError::Venue(format!(
            "unsupported alpaca options features: {detail}"
        )))
    }

    /// Render the advertised capabilities.
    pub fn to_capabilities(&self) -> Vec<VenueCapability> {
        vec![
            VenueCapability {
                feature: "order_class:mleg".into(),
                support: VenueCapabilitySupport::Supported,
            },
            VenueCapability {
                feature: format!("max_legs:{}", self.max_legs),
                support: VenueCapabilitySupport::Supported,
            },
            VenueCapability {
                feature: "time_in_force:ioc".into(),
                support: VenueCapabilitySupport::Unsupported,
            },
        ]
    }
}

fn rejection(
    reason: UnsupportedFeatureReason,
    feature: &str,
    detail: &str,
) -> CapabilityRejection {
    CapabilityRejection {
        reason,
        feature: feature.to_string(),
        detail: detail.to_string(),
    }
}

fn is_integral(value: Decimal18) -> bool {
    value
        .round_to(RoundingMode::Truncate, 0)
        .map(|rounded| rounded == value)
        .unwrap_or(false)
}

/// Validate that every leg is an OSI option on one underlying root.
pub fn require_single_underlying(order: &MultiLegOrder) -> LiveResult<()> {
    let mut root: Option<String> = None;
    for leg in &order.legs {
        let osi = crate::symbol::OsiSymbol::new(leg.symbol.as_str())
            .map_err(|_| LiveError::Venue("leg is not an OSI option contract".into()))?;
        match &root {
            None => root = Some(osi.root().to_string()),
            Some(existing) if existing != osi.root() => {
                return Err(LiveError::Venue(
                    "every leg must share one underlying root".into(),
                ));
            }
            Some(_) => {}
        }
    }
    Ok(())
}

/// Validate a symbol is an option contract.
pub fn require_option_symbol(symbol: &Symbol) -> LiveResult<()> {
    if is_osi_symbol(symbol.as_str()) {
        Ok(())
    } else {
        Err(LiveError::Venue("symbol must be an OSI option contract".into()))
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::AlpacaOptionsCapabilityMatrix;
    use crate::orders::{MultiLegOrder, OrderLeg, OrderType, Side, TimeInForce};
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn leg(symbol: &str, side: Side, quantity: &str) -> OrderLeg {
        OrderLeg::new(
            Symbol::new(symbol).unwrap(),
            side,
            dec(quantity),
            dec("1"),
            None,
        )
        .unwrap()
    }

    #[test]
    fn default_matrix_accepts_two_leg_day_vertical() {
        let order = MultiLegOrder::new(
            "g1",
            vec![
                leg("AAPL240119C00150000", Side::Buy, "1"),
                leg("AAPL240119C00160000", Side::Sell, "1"),
            ],
            OrderType::Limit,
            TimeInForce::Day,
            None,
            Some(dec("2")),
        )
        .unwrap();
        let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
        assert!(matrix.require(&order).is_ok());
    }

    #[test]
    fn ioc_and_fractional_ratios_are_rejected() {
        let order = MultiLegOrder::new(
            "g2",
            vec![
                leg("AAPL240119C00150000", Side::Buy, "1"),
                leg("AAPL240119C00160000", Side::Sell, "1"),
            ],
            OrderType::Limit,
            TimeInForce::Ioc,
            None,
            Some(dec("2")),
        )
        .unwrap();
        let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
        assert!(matrix.require(&order).is_err());
        assert!(!matrix.check(&order).is_empty());
    }
}
