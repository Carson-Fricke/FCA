use super::*;

#[test]
fn canonical_symbol_uppercases_and_strips() {
    assert_eq!(canonical_symbol("  aapl ").expect("valid"), "AAPL");
    assert_eq!(canonical_symbol("brk-b").expect("valid"), "BRK-B");
    assert_eq!(canonical_symbol("btc/usd").expect("valid"), "BTC/USD");
}

#[test]
fn canonical_symbol_rejects_invalid_shapes() {
    assert!(canonical_symbol("").is_err());
    assert!(canonical_symbol("A B").is_err());
    assert!(canonical_symbol("-X").is_err());
    assert!(canonical_symbol("é").is_err());
}

#[test]
fn osi_symbols_validate_compact_form() {
    assert_eq!(
        canonical_osi_symbol("spy   241220c00500000").expect("valid"),
        "SPY241220C00500000"
    );
    assert!(is_osi_symbol("SPY241220C00500000"));
    assert!(!is_osi_symbol("SPY241220X00500000"));
    assert!(canonical_osi_symbol("not-an-option").is_err());
}

#[test]
fn crypto_pair_accepts_dashed_and_slashed_forms() {
    assert_eq!(crypto_pair("btc-usd"), Some("BTC/USD".to_string()));
    assert_eq!(crypto_pair("BTC/USD"), Some("BTC/USD".to_string()));
    assert_eq!(crypto_pair("BRK-B"), None);
    assert_eq!(crypto_pair("AAPL"), None);
}
