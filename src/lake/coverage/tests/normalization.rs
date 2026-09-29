//! Input normalization and request-validation parity tests.

use lake_catalog::MemoryCatalogRepository;
use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::{calculate_coverage, requested_coverage, EnsureSpec};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, 0, 0, 0).expect("valid datetime")
}

fn symbols(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_string()).collect()
}

#[test]
fn symbols_and_data_types_are_canonicalized_deduped_and_ordered() {
    let report = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Equity,
        &[DataType::Trade, DataType::Quote, DataType::Trade],
        &symbols(&[" msft ", "aapl", "MSFT"]),
        None,
        day(2024, 1, 18),
        day(2024, 1, 19),
        instant(2024, 1, 20, 0),
    )
    .expect("coverage calculation");

    let order: Vec<(DataType, &str, Date)> = report
        .entries
        .iter()
        .map(|entry| (entry.data_type, entry.symbol.as_str(), entry.session_date()))
        .collect();
    assert_eq!(
        order,
        vec![
            (DataType::Quote, "AAPL", day(2024, 1, 18)),
            (DataType::Quote, "AAPL", day(2024, 1, 19)),
            (DataType::Quote, "MSFT", day(2024, 1, 18)),
            (DataType::Quote, "MSFT", day(2024, 1, 19)),
            (DataType::Trade, "AAPL", day(2024, 1, 18)),
            (DataType::Trade, "AAPL", day(2024, 1, 19)),
            (DataType::Trade, "MSFT", day(2024, 1, 18)),
            (DataType::Trade, "MSFT", day(2024, 1, 19)),
        ]
    );
}

#[test]
fn empty_symbol_lists_are_rejected() {
    let error = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &[],
        None,
        day(2024, 1, 18),
        day(2024, 1, 18),
        instant(2024, 1, 20, 0),
    )
    .expect_err("empty symbol lists are invalid");
    assert_eq!(error.code, "invalid_symbol");
    assert_eq!(error.to_string(), "symbols must contain non-empty strings");

    let blank = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &symbols(&["   "]),
        None,
        day(2024, 1, 18),
        day(2024, 1, 18),
        instant(2024, 1, 20, 0),
    )
    .expect_err("blank symbols are invalid");
    assert_eq!(blank.code, "invalid_symbol");
    assert_eq!(blank.to_string(), "symbols must contain non-empty strings");
}

#[test]
fn invalid_symbol_characters_are_rejected() {
    let error = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &symbols(&["bad!sym"]),
        None,
        day(2024, 1, 18),
        day(2024, 1, 18),
        instant(2024, 1, 20, 0),
    )
    .expect_err("invalid symbols are rejected");

    assert_eq!(error.code, "invalid_symbol");
    assert!(error.to_string().contains("invalid market symbol"));
}

#[test]
fn empty_data_type_lists_are_rejected() {
    let error = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Equity,
        &[],
        &symbols(&["AAPL"]),
        None,
        day(2024, 1, 18),
        day(2024, 1, 18),
        instant(2024, 1, 20, 0),
    )
    .expect_err("empty data type lists are invalid");

    assert_eq!(error.code, "invalid_data_types");
    assert_eq!(error.to_string(), "data_types must not be empty");
}

#[test]
fn reversed_date_ranges_are_rejected() {
    let error = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Equity,
        &[DataType::Trade],
        &symbols(&["AAPL"]),
        None,
        day(2024, 1, 19),
        day(2024, 1, 18),
        instant(2024, 1, 20, 0),
    )
    .expect_err("reversed ranges are invalid");

    assert_eq!(error.code, "invalid_date_range");
    assert_eq!(error.to_string(), "start must be on or before end");
}

#[test]
fn unsupported_asset_classes_use_the_operation_message() {
    let spec = EnsureSpec {
        asset_class: AssetClass::Option,
        data_type: DataType::Trade,
        symbol: "SPY240119C00500000".to_string(),
        start_date: day(2024, 1, 18),
        end_date: day(2024, 1, 19),
        interval: None,
    };
    let catalog = MemoryCatalogRepository::new();

    for (operation, expected) in [
        (
            "catalog",
            "coverage planning supports equity and crypto only",
        ),
        ("query", "file queries support equity and crypto only"),
        ("ensure", "ensure planning supports equity and crypto only"),
    ] {
        let error = requested_coverage(&catalog, None, &spec, instant(2024, 1, 22, 12), operation)
            .expect_err("option coverage is unsupported");
        assert_eq!(error.code, "unsupported_asset_class", "{operation}");
        assert_eq!(error.to_string(), expected, "{operation}");
    }
}

#[test]
fn calculate_coverage_rejects_unsupported_asset_classes_with_the_python_message() {
    let error = calculate_coverage(
        &MemoryCatalogRepository::new(),
        None,
        AssetClass::Option,
        &[DataType::Trade],
        &symbols(&["AAPL"]),
        None,
        day(2024, 1, 18),
        day(2024, 1, 18),
        instant(2024, 1, 20, 0),
    )
    .expect_err("option coverage is unsupported");

    assert_eq!(error.code, "unsupported_asset_class");
    assert_eq!(
        error.to_string(),
        "coverage planning supports equity and crypto only"
    );
}
