use super::*;

#[test]
fn market_symbols_canonicalize() {
    assert_eq!(canonical_symbol(" aapl ").unwrap(), "AAPL");
    assert_eq!(canonical_symbol("BTC/USD").unwrap(), "BTC/USD");
    assert!(canonical_symbol("").is_err());
    assert_eq!(canonical_symbol(" bad\t").unwrap(), "BAD");
    assert!(canonical_symbol("BA D").is_err());
}

#[test]
fn osi_symbols_canonicalize() {
    assert_eq!(
        canonical_osi_symbol(" aapl240119c00150000 ").unwrap(),
        "AAPL240119C00150000"
    );
    assert_eq!(
        canonical_osi_symbol("AAPL  240119C00150000").unwrap(),
        "AAPL240119C00150000"
    );
    assert!(is_osi_symbol("AAPL240119C00150000"));
    assert!(!is_osi_symbol("AAPL"));
}

#[test]
fn dashed_crypto_shapes_map_to_pairs() {
    assert_eq!(crypto_pair("BTC-USD").as_deref(), Some("BTC/USD"));
    assert_eq!(crypto_pair(" btc/usd ").as_deref(), Some("BTC/USD"));
    assert_eq!(crypto_pair("ETH-USD").as_deref(), Some("ETH/USD"));
    assert_eq!(crypto_pair("MSTR"), None);
    assert_eq!(crypto_pair("BRK.B"), None);
    assert_eq!(crypto_pair("AAPL240119C00150000"), None);
}
