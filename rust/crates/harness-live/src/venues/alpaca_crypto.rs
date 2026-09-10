//! Deterministic fractional crypto execution venue for Alpaca accounts.
//!
//! Quantities stay exact through the full command path, and every fill is
//! reconciled against the locally configured maker-taker model and the venue
//! fee schedule before any outcome reaches the kernel.

use harness_num::Decimal18;

use crate::accounting::Fill;
use crate::error::{require_text, LiveError, LiveResult};
use crate::fills::fees::{CryptoMakerTakerFeeModel, FeeRounding, LiquidityRole};
use crate::orders::Side;
use crate::symbol::Symbol;

const BASIS_POINTS: i64 = 10_000;

/// Return whether a canonical symbol is a `BASE/QUOTE` crypto pair.
pub fn is_crypto_pair(symbol: &str) -> bool {
    let mut parts = symbol.splitn(2, '/');
    match (parts.next(), parts.next()) {
        (Some(base), Some(quote)) => !base.is_empty() && !quote.is_empty() && !quote.contains('/'),
        _ => false,
    }
}

/// A venue-reported maker-taker crypto fee schedule fixture.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaCryptoFeeSchedule {
    /// Schedule identifier.
    pub schedule_id: String,
    /// ISO currency code.
    pub currency: String,
    /// Maker rate in basis points.
    pub maker_rate_bps: Decimal18,
    /// Taker rate in basis points.
    pub taker_rate_bps: Decimal18,
    /// Rounding rule.
    pub rounding: FeeRounding,
}

impl AlpacaCryptoFeeSchedule {
    /// Construct a validated schedule.
    pub fn new(
        schedule_id: &str,
        currency: &str,
        maker_rate_bps: Decimal18,
        taker_rate_bps: Decimal18,
        rounding: FeeRounding,
    ) -> LiveResult<Self> {
        if maker_rate_bps.is_negative() || taker_rate_bps.is_negative() {
            return Err(LiveError::Venue(
                "crypto fee rates must not be negative".into(),
            ));
        }
        if currency.len() != 3 || !currency.bytes().all(|byte| byte.is_ascii_uppercase()) {
            return Err(LiveError::Venue(
                "currency must be a three-letter uppercase code".into(),
            ));
        }
        Ok(Self {
            schedule_id: require_text(schedule_id, "schedule_id")?,
            currency: currency.to_string(),
            maker_rate_bps,
            taker_rate_bps,
            rounding,
        })
    }

    /// Return the schedule rate in basis points for one liquidity role.
    pub fn rate_for(&self, liquidity: LiquidityRole) -> Decimal18 {
        match liquidity {
            LiquidityRole::Maker => self.maker_rate_bps,
            LiquidityRole::Taker => self.taker_rate_bps,
        }
    }

    /// Return the venue-reported charge for one fill, exactly rounded.
    pub fn charge(
        &self,
        quantity: Decimal18,
        price: Decimal18,
        liquidity: LiquidityRole,
    ) -> LiveResult<Decimal18> {
        if !quantity.is_positive() || !price.is_positive() {
            return Err(LiveError::Venue(
                "quantity and price must be positive".into(),
            ));
        }
        let notional = quantity * price;
        Ok(self
            .rounding
            .apply(notional * self.rate_for(liquidity) / Decimal18::from_i64(BASIS_POINTS)))
    }
}

/// One reconciled local fee record for a single fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CryptoFeeLedgerEntry {
    /// The fill identifier.
    pub fill_id: String,
    /// The order identifier.
    pub order_id: String,
    /// The pair symbol.
    pub symbol: Symbol,
    /// Buy or sell.
    pub side: Side,
    /// Exact quantity.
    pub quantity: Decimal18,
    /// Exact price.
    pub price: Decimal18,
    /// The resolved liquidity role.
    pub liquidity: LiquidityRole,
    /// The locally configured fee.
    pub local_fee: Decimal18,
    /// The venue-reported fee.
    pub venue_fee: Decimal18,
}

/// The deterministic crypto venue shell with fee reconciliation.
#[derive(Clone, Debug)]
pub struct AlpacaCryptoVenue {
    local: CryptoMakerTakerFeeModel,
    schedule: AlpacaCryptoFeeSchedule,
    ledger: Vec<CryptoFeeLedgerEntry>,
}

impl AlpacaCryptoVenue {
    /// Construct a venue from a local fee model and a venue schedule.
    pub fn new(local: CryptoMakerTakerFeeModel, schedule: AlpacaCryptoFeeSchedule) -> Self {
        Self {
            local,
            schedule,
            ledger: Vec::new(),
        }
    }

    /// The reconciled fee ledger.
    pub fn fee_ledger(&self) -> &[CryptoFeeLedgerEntry] {
        &self.ledger
    }

    /// Reconcile one fill's local fee against the venue schedule.
    ///
    /// A disagreement is recorded in the ledger and surfaced as an error so it
    /// is never silently tolerated.
    pub fn reconcile_fee(&mut self, fill: &Fill) -> LiveResult<Decimal18> {
        if !is_crypto_pair(fill.symbol.as_str()) {
            return Err(LiveError::Venue(
                "crypto fills require a BASE/QUOTE pair symbol".into(),
            ));
        }
        let Some(liquidity_text) = fill.liquidity.as_deref() else {
            return Err(LiveError::Venue(
                "crypto fills require maker or taker liquidity".into(),
            ));
        };
        let liquidity = LiquidityRole::parse(liquidity_text)?;
        let local_fee = self
            .local
            .calculate(fill)?
            .iter()
            .fold(Decimal18::ZERO, |sum, component| sum + component.amount);
        let venue_fee = self.schedule.charge(fill.quantity, fill.price, liquidity)?;
        self.ledger.push(CryptoFeeLedgerEntry {
            fill_id: fill.fill_id.clone(),
            order_id: fill.order_id.clone(),
            symbol: fill.symbol.clone(),
            side: fill.side,
            quantity: fill.quantity,
            price: fill.price,
            liquidity,
            local_fee,
            venue_fee,
        });
        if local_fee != venue_fee {
            return Err(LiveError::Venue(format!(
                "crypto fee mismatch for fill {}: local {} vs venue {}",
                fill.fill_id, local_fee, venue_fee
            )));
        }
        Ok(local_fee)
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::{is_crypto_pair, AlpacaCryptoFeeSchedule, AlpacaCryptoVenue};
    use crate::accounting::Fill;
    use crate::fills::fees::{CryptoMakerTakerFeeModel, FeeRounding, FeeRoundingMode};
    use crate::orders::Side;
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn fill(quantity: &str, price: &str, liquidity: &str) -> Fill {
        Fill::new(
            "f1",
            "o1",
            Symbol::new("BTC/USD").unwrap(),
            Side::Buy,
            dec(quantity),
            dec(price),
            Decimal18::ZERO,
            Some(liquidity.to_string()),
        )
        .unwrap()
    }

    #[test]
    fn crypto_pair_detection() {
        assert!(is_crypto_pair("BTC/USD"));
        assert!(!is_crypto_pair("AAPL"));
        assert!(!is_crypto_pair("BTC/USD/EUR"));
    }

    #[test]
    fn reconciling_matching_fees_succeeds() {
        let rounding = FeeRounding::new(dec("0.01"), FeeRoundingMode::HalfEven).unwrap();
        let local = CryptoMakerTakerFeeModel::new(rounding, dec("0.10"), dec("0.25")).unwrap();
        let schedule = AlpacaCryptoFeeSchedule::new(
            "alpaca-crypto",
            "USD",
            dec("0.10"),
            dec("0.25"),
            rounding,
        )
        .unwrap();
        let mut venue = AlpacaCryptoVenue::new(local, schedule);
        let fee = venue.reconcile_fee(&fill("200", "100", "taker")).unwrap();
        assert_eq!(fee, dec("0.50"));
        assert_eq!(venue.fee_ledger().len(), 1);
    }

    #[test]
    fn mismatched_fees_surface_an_error() {
        let rounding = FeeRounding::new(dec("0.01"), FeeRoundingMode::HalfEven).unwrap();
        let local = CryptoMakerTakerFeeModel::new(rounding, dec("0.10"), dec("0.25")).unwrap();
        let schedule = AlpacaCryptoFeeSchedule::new(
            "alpaca-crypto",
            "USD",
            dec("0.10"),
            dec("0.30"),
            rounding,
        )
        .unwrap();
        let mut venue = AlpacaCryptoVenue::new(local, schedule);
        assert!(venue.reconcile_fee(&fill("200", "100", "taker")).is_err());
        assert_eq!(venue.fee_ledger().len(), 1);
    }
}
