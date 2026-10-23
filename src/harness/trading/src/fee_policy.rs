//! Owns checked raw fee formulas shared by replay and live execution.
//! Representation adapters own rounding, attribution and configuration validation.
use crate::ledger::Money;
use num::DecimalError;
use std::cmp::Ordering;
#[derive(Debug)]
pub enum FeeError {
    Arithmetic(&'static str, DecimalError),
    FractionalContracts,
}
impl std::fmt::Display for FeeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Arithmetic(component, source) => write!(f, "{component}: {source}"),
            Self::FractionalContracts => {
                f.write_str("option fill quantity must be a whole number of contracts")
            }
        }
    }
}
fn failure(component: &'static str) -> impl FnOnce(DecimalError) -> FeeError {
    move |source| FeeError::Arithmetic(component, source)
}

/// Passive rates; adapters validate their configuration before calculation.
pub struct EquityRates<T> {
    pub commission_per_share: T,
    pub commission_minimum: T,
    pub sec_rate_bps: T,
    pub taf_per_share: T,
    pub taf_maximum: T,
}
pub fn equity<T: Money>(
    quantity: T,
    price: T,
    sell: bool,
    rates: EquityRates<T>,
    basis_points: T,
) -> Result<Vec<(&'static str, T)>, FeeError> {
    let EquityRates {
        commission_per_share: per_share,
        commission_minimum: minimum,
        sec_rate_bps: sec_bps,
        taf_per_share,
        taf_maximum,
    } = rates;
    let raw = quantity.mul(per_share).map_err(failure("commission"))?;
    let commission = if raw.compare(minimum) == Ordering::Less {
        minimum
    } else {
        raw
    };
    let mut components = vec![("commission", commission)];
    if sell {
        let sec = quantity
            .mul(price)
            .and_then(|v| v.mul(sec_bps))
            .and_then(|v| v.div(basis_points))
            .map_err(failure("SEC regulatory fee"))?;
        let raw = quantity.mul(taf_per_share).map_err(failure("FINRA TAF"))?;
        let taf = if raw.compare(taf_maximum) == Ordering::Greater {
            taf_maximum
        } else {
            raw
        };
        components.extend([("sec-regulatory", sec), ("finra-taf", taf)]);
    }
    Ok(components)
}
pub fn options<T: Money>(
    quantity: T,
    commission: T,
    fee: T,
) -> Result<Vec<(&'static str, T)>, FeeError> {
    if !quantity.is_whole() {
        return Err(FeeError::FractionalContracts);
    }
    Ok(vec![
        (
            "contract-commission",
            quantity
                .mul(commission)
                .map_err(failure("contract commission"))?,
        ),
        (
            "contract-fee",
            quantity.mul(fee).map_err(failure("contract fee"))?,
        ),
    ])
}
pub fn crypto<T: Money>(
    quantity: T,
    price: T,
    rate: T,
    maker: bool,
    basis_points: T,
) -> Result<Vec<(&'static str, T)>, FeeError> {
    Ok(vec![(
        if maker { "maker-fee" } else { "taker-fee" },
        quantity
            .mul(price)
            .and_then(|v| v.mul(rate))
            .and_then(|v| v.div(basis_points))
            .map_err(failure("crypto fee"))?,
    )])
}
