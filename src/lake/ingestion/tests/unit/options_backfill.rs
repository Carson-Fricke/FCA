use super::*;
use std::collections::BTreeMap;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

#[test]
fn data_type_normalization_follows_the_canonical_order() {
    assert_eq!(
        normalize_data_types(None).expect("default"),
        vec![DataType::Trade, DataType::Quote]
    );
    assert_eq!(
        normalize_data_types(Some(&[DataType::Quote])).expect("quote"),
        vec![DataType::Quote]
    );
    let error = normalize_data_types(Some(&[])).expect_err("empty");
    assert_eq!(error.to_string(), "data_types must not be empty");
    let error = normalize_data_types(Some(&[DataType::Classification])).expect_err("unsupported");
    assert_eq!(
        error.to_string(),
        "option data_types must be trades or quotes"
    );
}

#[test]
fn underlyings_are_canonicalized_deduplicated_and_sorted() {
    let universe = OptionsUniverse {
        feed: "opra".to_string(),
        data_types: vec!["trade".to_string(), "quote".to_string()],
        selection: lake_configuration::OptionSelection {
            frequency: "daily".to_string(),
            persist_front_set: true,
            expiration_order: "nearest".to_string(),
            expiration_count: 2,
            strike_window: lake_configuration::StrikeWindow {
                basis: "underlying_spot".to_string(),
                percent_below: 10.0,
                percent_above: 10.0,
            },
        },
        soak: lake_configuration::OptionsSoak {
            underlyings: vec!["SPY".to_string(), "QQQ".to_string()],
            lookback_years: 2.0,
        },
        expansion: lake_configuration::OptionsExpansion {
            enabled: false,
            maximum_underlyings: 10,
        },
    };
    assert_eq!(
        configuration_underlyings(None, Some(&universe)).expect("universe"),
        vec!["QQQ".to_string(), "SPY".to_string()]
    );
    assert_eq!(
        configuration_underlyings(
            Some(&["spy".to_string(), "QQQ".to_string(), " SPY ".to_string()]),
            None
        )
        .expect("explicit"),
        vec!["QQQ".to_string(), "SPY".to_string()]
    );
    let error = configuration_underlyings(None, None).expect_err("missing source");
    assert_eq!(
        error.to_string(),
        "underlyings or universe must be supplied"
    );
    let error = configuration_underlyings(Some(&[" ".to_string()]), None).expect_err("blank");
    assert!(error.to_string().contains("non-empty strings"));
}

#[test]
fn clock_sources_are_exclusive() {
    const FIXED: DateTime = DateTime::EPOCH;
    let clock: OptionClock =
        Arc::new(|| DateTime::from_ymd_hms(2024, 1, 20, 0, 0, 0, 0).expect("instant"));
    assert_eq!(resolve_clock(Some(FIXED), None).expect("as_of"), FIXED);
    assert_eq!(
        resolve_clock(None, Some(&clock)).expect("clock"),
        DateTime::from_ymd_hms(2024, 1, 20, 0, 0, 0, 0).expect("instant")
    );
    let error = resolve_clock(Some(FIXED), Some(&clock)).expect_err("two sources");
    assert_eq!(
        error.to_string(),
        "as_of and clock must not both be supplied"
    );
}

#[test]
fn plan_rejects_bad_bounds_and_limits() {
    let session = CoverageSession {
        session_date: day(2024, 1, 19),
        open_time: DateTime::from_ymd_hms(2024, 1, 19, 14, 30, 0, 0).expect("open"),
        close_time: DateTime::from_ymd_hms(2024, 1, 19, 21, 0, 0, 0).expect("close"),
        is_partial: false,
    };
    let contract = lake_catalog::ContractRecord::new(
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        lake_catalog::OptionType::Call,
        100,
        None,
        None,
    )
    .expect("contract");
    let item = OptionBackfillItem {
        contract,
        session,
        data_types: vec![DataType::Trade, DataType::Quote],
        replace_existing: false,
    };

    let start = day(2024, 1, 19);
    let end = day(2024, 1, 18);
    let error =
        OptionBackfillPlan::new(start, end, Vec::new(), Vec::new(), 0).expect_err("reversed");
    assert_eq!(error.to_string(), "start must be on or before end");

    // A work limit below the item count is a construction error; an
    // `available_items` above it is the documented truncation shape.
    let error = OptionBackfillPlan::new(start, start, vec![item.clone(), item], Vec::new(), 1)
        .expect_err("available below items");
    assert_eq!(
        error.to_string(),
        "available_items must include the selected items"
    );
    OptionBackfillPlan::new(start, start, Vec::new(), Vec::new(), 1).expect("truncated plan");
}

#[test]
fn session_dates_preserve_dispatch_order_without_duplicates() {
    let session = CoverageSession {
        session_date: day(2024, 1, 19),
        open_time: DateTime::from_ymd_hms(2024, 1, 19, 14, 30, 0, 0).expect("open"),
        close_time: DateTime::from_ymd_hms(2024, 1, 19, 21, 0, 0, 0).expect("close"),
        is_partial: false,
    };
    let contract = lake_catalog::ContractRecord::new(
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        lake_catalog::OptionType::Call,
        100,
        None,
        None,
    )
    .expect("contract");
    let item = OptionBackfillItem {
        contract,
        session,
        data_types: vec![DataType::Trade, DataType::Quote],
        replace_existing: false,
    };
    let plan = OptionBackfillPlan::new(
        day(2024, 1, 19),
        day(2024, 1, 19),
        vec![item.clone(), item],
        Vec::new(),
        2,
    )
    .expect("plan");
    assert_eq!(plan.session_dates(), vec![day(2024, 1, 19)]);
    assert!(!plan.has_more());
    assert_eq!(plan.items().len(), 2);
}

#[test]
fn queue_messages_carry_the_complete_provider_independent_payload() {
    let session = CoverageSession {
        session_date: day(2024, 1, 19),
        open_time: DateTime::from_ymd_hms(2024, 1, 19, 14, 30, 0, 0).expect("open"),
        close_time: DateTime::from_ymd_hms(2024, 1, 19, 21, 0, 0, 0).expect("close"),
        is_partial: false,
    };
    let contract = lake_catalog::ContractRecord::new(
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        lake_catalog::OptionType::Call,
        100,
        None,
        None,
    )
    .expect("contract");
    let item = OptionBackfillItem {
        contract,
        session,
        data_types: vec![DataType::Trade, DataType::Quote],
        replace_existing: true,
    };
    assert_eq!(
        item.queue_message(),
        serde_json::json!({
            "asset_class": "option",
            "data_types": ["trade", "quote"],
            "end_time": "2024-01-19T21:00:00Z",
            "expiration_date": "2024-01-19",
            "option_type": "call",
            "replace_existing": true,
            "session_date": "2024-01-19",
            "start_time": "2024-01-19T14:30:00Z",
            "strike_price": "500",
            "symbol": "SPY240119C00500000",
            "underlying": "SPY",
        })
    );
}

#[test]
fn partial_coverage_requests_replacement_and_terminal_rows_do_not() {
    let mut coverage: BTreeMap<(String, Date, DataType), CoverageRecord> = BTreeMap::new();
    let partial = CoverageRecord::new(
        AssetClass::Option,
        DataType::Trade,
        "SPY240119C00500000",
        day(2024, 1, 19),
        CoverageStatus::Partial,
        None,
        0,
        0,
        None,
        None,
    )
    .expect("record");
    coverage.insert(
        (
            partial.symbol.clone(),
            partial.session_date,
            partial.data_type,
        ),
        partial,
    );
    let empty = CoverageRecord::new(
        AssetClass::Option,
        DataType::Quote,
        "SPY240119C00500000",
        day(2024, 1, 19),
        CoverageStatus::Empty,
        None,
        0,
        0,
        None,
        None,
    )
    .expect("record");
    coverage.insert(
        (empty.symbol.clone(), empty.session_date, empty.data_type),
        empty,
    );

    let data_types = [DataType::Trade, DataType::Quote];
    assert!(replace_existing(
        &coverage,
        "SPY240119C00500000",
        day(2024, 1, 19),
        &data_types
    ));
    assert!(!is_terminal(
        &coverage,
        "SPY240119C00500000",
        day(2024, 1, 19),
        &data_types
    ));

    let complete = CoverageRecord::new(
        AssetClass::Option,
        DataType::Trade,
        "SPY240119C00500000",
        day(2024, 1, 19),
        CoverageStatus::Complete,
        None,
        1,
        1,
        None,
        None,
    )
    .expect("record");
    coverage.insert(
        (
            complete.symbol.clone(),
            complete.session_date,
            complete.data_type,
        ),
        complete,
    );
    assert!(is_terminal(
        &coverage,
        "SPY240119C00500000",
        day(2024, 1, 19),
        &data_types
    ));
}

#[test]
fn datetime_format_matches_the_python_iso_rendering() {
    let whole = DateTime::from_ymd_hms(2024, 1, 19, 21, 0, 0, 0).expect("instant");
    assert_eq!(format_datetime(whole), "2024-01-19T21:00:00Z");
    let micros = DateTime::from_ymd_hms(2024, 1, 19, 21, 0, 0, 500_000).expect("instant");
    assert_eq!(format_datetime(micros), "2024-01-19T21:00:00.500000Z");
}
