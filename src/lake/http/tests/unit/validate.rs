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
    assert_eq!(
        parse_data_type("bars").expect("bars"),
        lake_contracts::DataType::Bars
    );
    assert!(parse_asset_class("futures").is_err());
    assert!(parse_data_type("candles").is_err());
}

#[test]
fn intervals_are_validated_against_the_data_type() {
    use lake_contracts::DataType;

    assert_eq!(parse_interval(DataType::Trade, None).expect("none"), None);
    assert_eq!(
        parse_interval(DataType::Bars, Some("1min".to_string())).expect("minute"),
        Some("1min".to_string())
    );
    assert_eq!(
        parse_interval(DataType::Bars, Some("1d".to_string())).expect("day"),
        Some("1d".to_string())
    );

    let missing = parse_interval(DataType::Bars, None).expect_err("bars needs an interval");
    assert_eq!(
        missing.details,
        Some(json!([{
            "loc": ["interval"],
            "msg": "Value error, interval is required for bars",
            "type": "value_error",
        }]))
    );

    let unknown =
        parse_interval(DataType::Bars, Some("5m".to_string())).expect_err("unknown token");
    assert!(unknown
        .details
        .expect("details")
        .to_string()
        .contains("invalid bar interval: \\\"5m\\\"; expected one of 1min, 1d"));

    let misplaced =
        parse_interval(DataType::Quote, Some("1min".to_string())).expect_err("quote interval");
    assert!(misplaced
        .details
        .expect("details")
        .to_string()
        .contains("interval is supported for bars only"));

    assert!(reject_option_data_type(DataType::Trade).is_ok());
    let option_bars =
        reject_option_data_type(DataType::Bars).expect_err("option bars are unsupported");
    assert!(option_bars
        .details
        .expect("details")
        .to_string()
        .contains("not supported for options"));
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
