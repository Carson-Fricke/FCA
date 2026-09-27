use super::*;

fn session_date() -> Date {
    Date::from_ymd(2024, 1, 19).expect("session date")
}

#[test]
fn scope_canonicalizes_symbols_and_builds_stable_names() {
    let scope = SymbolDayScope::new(AssetClass::Equity, " aapl ", session_date()).expect("scope");

    assert_eq!(scope.asset_class, AssetClass::Equity);
    assert_eq!(scope.symbol, "AAPL");
    assert_eq!(scope.session_date, session_date());
    assert_eq!(scope.key(), "equity/2024-01-19/AAPL");
    assert_eq!(
        scope.blob_name(),
        "date=2024-01-19/asset_class=equity/AAPL.lock"
    );
}

#[test]
fn scope_quotes_symbols_like_python_quote_with_safe_chars() {
    let slash = SymbolDayScope::new(AssetClass::Crypto, "btc/usd", session_date()).expect("scope");
    assert_eq!(slash.key(), "crypto/2024-01-19/BTC%2FUSD");
    assert_eq!(
        slash.blob_name(),
        "date=2024-01-19/asset_class=crypto/BTC%2FUSD.lock"
    );

    let dotted = SymbolDayScope::new(AssetClass::Equity, "BRK.B", session_date()).expect("scope");
    assert_eq!(dotted.key(), "equity/2024-01-19/BRK.B");

    let dashed = SymbolDayScope::new(AssetClass::Equity, "BRK-B", session_date()).expect("scope");
    assert_eq!(dashed.key(), "equity/2024-01-19/BRK-B");
}

#[test]
fn scope_rejects_invalid_symbols() {
    for symbol in ["", "..", "!!", "-LEAD", "AA PL"] {
        let error =
            SymbolDayScope::new(AssetClass::Equity, symbol, session_date()).expect_err(symbol);
        assert!(
            matches!(error, DataError::ValueError(_)),
            "{symbol:?}: {error:?}"
        );
    }
}
