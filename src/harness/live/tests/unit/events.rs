use num::Decimal18;

use super::{MarketEvent, QuoteEvent};
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

#[test]
fn quote_requires_positive_prices() {
    let symbol = Symbol::new("AAPL").unwrap();
    assert!(QuoteEvent::new(
        symbol.clone(),
        dec("100"),
        dec("1"),
        dec("101"),
        dec("1"),
        None
    )
    .is_ok());
    assert!(QuoteEvent::new(symbol, dec("0"), dec("1"), dec("101"), dec("1"), None).is_err());
}

#[test]
fn market_event_exposes_symbol() {
    let event = MarketEvent::Quote(
        QuoteEvent::new(
            Symbol::new("AAPL").unwrap(),
            dec("100"),
            dec("1"),
            dec("101"),
            dec("1"),
            None,
        )
        .unwrap(),
    );
    assert_eq!(event.symbol().as_str(), "AAPL");
    assert!(event.as_quote().is_some());
    assert!(event.as_trade().is_none());
}
