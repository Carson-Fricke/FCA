//! Field validators and strict JSON readers.

use serde_json::json;

use super::*;

#[test]
fn symbols_must_start_alphanumeric_and_stay_path_safe() {
    assert_eq!(
        validate_symbol_field("AAPL", "symbol").expect("valid"),
        "AAPL"
    );
    assert_eq!(
        validate_symbol_field("BTC/USD", "symbol").expect("valid"),
        "BTC/USD"
    );
    assert!(validate_symbol_field("../AAPL", "symbol").is_err());
    assert!(validate_symbol_field("", "symbol").is_err());
    assert!(validate_symbol_field(" AAPL", "symbol").is_err());
    assert!(validate_symbol_field(&"A".repeat(65), "symbol").is_err());
}

#[test]
fn enumerated_values_are_parsed_by_persisted_name() {
    assert_eq!(
        parse_asset_class("equity").expect("equity"),
        lake_contracts::AssetClass::Equity
    );
    assert_eq!(
        parse_data_type("trade").expect("trade"),
        lake_contracts::DataType::Trade
    );
    assert!(parse_asset_class("futures").is_err());
    assert!(parse_data_type("bars").is_err());
}

#[test]
fn dates_parse_and_ranges_must_be_ordered() {
    let start = parse_date("2024-01-01", "start_date").expect("valid");
    let end = parse_date("2024-01-02", "end_date").expect("valid");
    assert!(validate_date_range(start, end).is_ok());
    assert!(validate_date_range(end, start).is_err());
    assert!(parse_date("not-a-date", "start_date").is_err());
    assert!(parse_date("2024-02-30", "start_date").is_err());
}

#[test]
fn strict_readers_reject_unknown_and_mistyped_fields() {
    let object = json!({
        "asset_class": "equity",
        "extra": true,
        "count": 3,
    });
    let object = object.as_object().expect("object");
    assert!(reject_unknown(object, &["asset_class"]).is_err());
    assert!(reject_unknown(object, &["asset_class", "extra", "count"]).is_ok());
    assert!(required_string(object, "asset_class").is_ok());
    assert!(required_string(object, "missing").is_err());
    assert!(optional_string(object, "count").is_err());
    assert!(optional_string(object, "missing").is_ok());
}

#[test]
fn symbol_lists_are_bounded_and_validated() {
    assert!(validate_symbol_list(vec!["spy".to_string()], "contract_symbols").is_ok());
    assert!(validate_symbol_list(Vec::new(), "contract_symbols").is_err());
    assert!(validate_symbol_list(vec!["..".to_string()], "contract_symbols").is_err());
    assert!(validate_symbol_list(vec!["A".to_string(); 101], "contract_symbols").is_err());
}
