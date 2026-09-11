use super::{canonical_symbol, is_osi_symbol, OsiSymbol, Symbol};
use num::Decimal18;

#[test]
fn market_symbols_are_canonicalized() {
    assert_eq!(Symbol::new(" aapl ").unwrap().as_str(), "AAPL");
    assert!(Symbol::new("aapl!").is_err());
    assert!(Symbol::new("").is_err());
}

#[test]
fn osi_symbols_parse_compact_and_padded() {
    let compact = OsiSymbol::new("AAPL240119C00150000").unwrap();
    assert_eq!(compact.root(), "AAPL");
    assert_eq!(compact.expiration(), "240119");
    assert_eq!(compact.option_type(), 'C');
    assert_eq!(compact.strike().to_string(), "150.000000000000000000");
    let padded = OsiSymbol::new("AAPL  240119C00150000").unwrap();
    assert_eq!(padded.as_str(), compact.as_str());
    assert!(is_osi_symbol("AAPL240119C00150000"));
    assert!(!is_osi_symbol("AAPL"));
}

#[test]
fn canonical_symbol_prefers_osi() {
    let symbol = canonical_symbol("AAPL240119P00150000").unwrap();
    assert_eq!(symbol.as_str(), "AAPL240119P00150000");
    let _ = Decimal18::ZERO;
}
