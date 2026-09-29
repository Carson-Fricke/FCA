use std::path::Path;

use serde_json::json;

use super::*;

fn fixture_payload() -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/company_tickers.json");
    std::fs::read(path).expect("fixture")
}

#[test]
fn recorded_fixture_parses_into_the_ticker_map() {
    let tickers = CompanyTickers::from_bytes(&fixture_payload()).expect("tickers");

    assert_eq!(tickers.len(), 4);
    assert!(!tickers.is_empty());
    assert_eq!(tickers.cik_for("AAPL"), Some(320193));
    assert_eq!(tickers.cik_for(" aapl "), Some(320193));
    assert_eq!(tickers.cik_for("MSFT"), Some(789019));
    assert_eq!(tickers.cik_for("GOOG"), Some(1652044));
    assert_eq!(tickers.cik_for("TSLA"), Some(1318605));
    assert_eq!(tickers.cik_for("ZVZZT"), None);
}

#[test]
fn the_default_map_resolves_nothing() {
    let tickers = CompanyTickers::default();

    assert_eq!(tickers.len(), 0);
    assert!(tickers.is_empty());
    assert_eq!(tickers.cik_for("AAPL"), None);
}

#[test]
fn duplicate_tickers_resolve_to_the_smallest_cik() {
    let document = json!({
        "0": {"cik_str": 200, "ticker": "DUP", "title": "Second"},
        "1": {"cik_str": 100, "ticker": "DUP", "title": "First"},
    });
    let tickers = CompanyTickers::from_document(&document).expect("tickers");

    assert_eq!(tickers.len(), 1);
    assert_eq!(tickers.cik_for("DUP"), Some(100));
}

#[test]
fn unknown_entry_fields_are_ignored() {
    let document = json!({
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc.", "extra": true},
    });
    let tickers = CompanyTickers::from_document(&document).expect("tickers");

    assert_eq!(tickers.cik_for("AAPL"), Some(320193));
}

#[test]
fn invalid_json_payloads_are_rejected() {
    let error = CompanyTickers::from_bytes(b"not json").expect_err("rejected");

    assert!(matches!(error, ReferenceError::Tickers(_)));
    assert!(error.to_string().contains("not valid JSON"));
}

#[test]
fn malformed_documents_are_rejected_entry_by_entry() {
    let cases = [
        json!([{"cik_str": 1, "ticker": "AAPL"}]),
        json!({"0": "not an object"}),
        json!({"0": {"cik_str": 320193}}),
        json!({"0": {"ticker": "AAPL"}}),
        json!({"0": {"cik_str": true, "ticker": "AAPL"}}),
        json!({"0": {"cik_str": -1, "ticker": "AAPL"}}),
        json!({"0": {"cik_str": 0, "ticker": "AAPL"}}),
        json!({"0": {"cik_str": 320193.5, "ticker": "AAPL"}}),
        json!({"0": {"cik_str": 320193, "ticker": ""}}),
        json!({"0": {"cik_str": 320193, "ticker": null}}),
    ];

    for case in cases {
        let error = CompanyTickers::from_document(&case).expect_err("rejected");
        assert!(matches!(error, ReferenceError::Tickers(_)), "{case}");
    }
}
