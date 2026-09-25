use super::*;
use lake_catalog::OptionType;
use std::cmp::Ordering;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(hour: u32, minute: u32, second: u32, micros: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, hour, minute, second, micros).expect("valid instant")
}

fn session() -> CoverageSession {
    CoverageSession {
        session_date: day(2024, 1, 19),
        open_time: instant(14, 30, 0, 0),
        close_time: instant(21, 0, 0, 0),
        is_partial: false,
    }
}

fn contract() -> ContractRecord {
    ContractRecord::new(
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        100,
        None,
        None,
    )
    .expect("contract")
}

fn trade(timestamp: DateTime) -> NormalizedOptionTrade {
    NormalizedOptionTrade {
        timestamp,
        symbol: "SPY240119C00500000".to_string(),
        underlying: "SPY".to_string(),
        expiration_date: day(2024, 1, 19),
        strike_price: Decimal18::parse("500").expect("strike"),
        option_type: OptionType::Call,
        exchange: "XOPR".to_string(),
        price: Decimal18::parse("2.50").expect("price"),
        size: 3,
        condition: "I".to_string(),
    }
}

fn quote(timestamp: DateTime) -> NormalizedOptionQuote {
    NormalizedOptionQuote {
        timestamp,
        symbol: "SPY240119C00500000".to_string(),
        underlying: "SPY".to_string(),
        expiration_date: day(2024, 1, 19),
        strike_price: Decimal18::parse("500").expect("strike"),
        option_type: OptionType::Call,
        bid_exchange: "XOPR".to_string(),
        bid_price: Decimal18::parse("2.40").expect("bid"),
        bid_size: 4,
        ask_exchange: "CBOE".to_string(),
        ask_price: Decimal18::parse("2.60").expect("ask"),
        ask_size: 5,
        condition: "A".to_string(),
    }
}

#[test]
fn option_trades_compare_on_every_pinned_tie_breaker() {
    let base = trade(instant(14, 30, 0, 0));
    assert_eq!(compare_option_trades(&base, &base), Ordering::Equal);

    let later = trade(instant(14, 31, 0, 0));
    assert_eq!(compare_option_trades(&base, &later), Ordering::Less);

    let mut other_exchange = trade(instant(14, 30, 0, 0));
    other_exchange.exchange = "CBOE".to_string();
    assert_eq!(
        compare_option_trades(&other_exchange, &base),
        Ordering::Less
    );

    let mut cheaper = trade(instant(14, 30, 0, 0));
    cheaper.price = Decimal18::parse("1").expect("price");
    assert_eq!(compare_option_trades(&cheaper, &base), Ordering::Less);

    let mut smaller = trade(instant(14, 30, 0, 0));
    smaller.size = 1;
    assert_eq!(compare_option_trades(&smaller, &base), Ordering::Less);

    let mut other_condition = trade(instant(14, 30, 0, 0));
    other_condition.condition = "J".to_string();
    assert_eq!(
        compare_option_trades(&base, &other_condition),
        Ordering::Less
    );
}

#[test]
fn option_quotes_compare_on_every_pinned_tie_breaker() {
    let base = quote(instant(14, 30, 0, 0));
    assert_eq!(compare_option_quotes(&base, &base), Ordering::Equal);

    let later = quote(instant(14, 31, 0, 0));
    assert_eq!(compare_option_quotes(&base, &later), Ordering::Less);

    let mut other_bid_exchange = quote(instant(14, 30, 0, 0));
    other_bid_exchange.bid_exchange = "CBOE".to_string();
    assert_eq!(
        compare_option_quotes(&other_bid_exchange, &base),
        Ordering::Less
    );

    let mut cheaper_bid = quote(instant(14, 30, 0, 0));
    cheaper_bid.bid_price = Decimal18::parse("1").expect("bid");
    assert_eq!(compare_option_quotes(&cheaper_bid, &base), Ordering::Less);

    let mut smaller_bid = quote(instant(14, 30, 0, 0));
    smaller_bid.bid_size = 1;
    assert_eq!(compare_option_quotes(&smaller_bid, &base), Ordering::Less);

    let mut other_ask_exchange = quote(instant(14, 30, 0, 0));
    other_ask_exchange.ask_exchange = "XOPR".to_string();
    assert_eq!(
        compare_option_quotes(&base, &other_ask_exchange),
        Ordering::Less
    );

    let mut cheaper_ask = quote(instant(14, 30, 0, 0));
    cheaper_ask.ask_price = Decimal18::parse("1").expect("ask");
    assert_eq!(compare_option_quotes(&cheaper_ask, &base), Ordering::Less);

    let mut smaller_ask = quote(instant(14, 30, 0, 0));
    smaller_ask.ask_size = 1;
    assert_eq!(compare_option_quotes(&smaller_ask, &base), Ordering::Less);

    let mut other_condition = quote(instant(14, 30, 0, 0));
    other_condition.condition = "B".to_string();
    assert_eq!(
        compare_option_quotes(&base, &other_condition),
        Ordering::Less
    );
}

#[test]
fn normalization_selects_both_data_types_in_canonical_order() {
    let selected = normalize_data_types(None).expect("default");
    assert_eq!(selected, vec![DataType::Trade, DataType::Quote]);

    let selected = normalize_data_types(Some(&[DataType::Quote, DataType::Trade])).expect("order");
    assert_eq!(selected, vec![DataType::Trade, DataType::Quote]);

    let selected = normalize_data_types(Some(&[DataType::Quote])).expect("quote only");
    assert_eq!(selected, vec![DataType::Quote]);

    let error = normalize_data_types(Some(&[])).expect_err("empty");
    assert_eq!(error.to_string(), "data_types must not be empty");

    let error = normalize_data_types(Some(&[DataType::Fundamentals])).expect_err("unsupported");
    assert_eq!(
        error.to_string(),
        "option data_types must be trades or quotes"
    );
}

#[test]
fn coverage_status_maps_partial_empty_and_complete() {
    assert_eq!(coverage_status(true, 5), CoverageStatus::Partial);
    assert_eq!(coverage_status(false, 0), CoverageStatus::Empty);
    assert_eq!(coverage_status(false, 3), CoverageStatus::Complete);
}

#[test]
fn record_validation_rejects_identity_and_window_mismatches() {
    let contract = contract();
    let session = session();
    validate_record(
        instant(14, 30, 0, 0),
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect("valid record");

    let error = validate_record(
        instant(14, 29, 59, 999_999),
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect_err("before open");
    assert!(
        error
            .to_string()
            .contains("record timestamp is outside session 2024-01-19"),
        "{error}"
    );

    let error = validate_record(
        instant(21, 0, 0, 500_000),
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect_err("after close");
    assert!(error.to_string().contains("outside session"));

    let error = validate_record(
        instant(14, 30, 0, 0),
        "QQQ240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect_err("symbol");
    assert!(error.to_string().contains("symbol does not match"));

    let error = validate_record(
        instant(14, 30, 0, 0),
        "SPY240119C00500000",
        "QQQ",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect_err("underlying");
    assert!(error.to_string().contains("underlying does not match"));

    let error = validate_record(
        instant(14, 30, 0, 0),
        "SPY240119C00500000",
        "SPY",
        day(2024, 2, 16),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect_err("expiration");
    assert!(error.to_string().contains("expiration does not match"));

    let error = validate_record(
        instant(14, 30, 0, 0),
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("505").expect("strike"),
        OptionType::Call,
        &contract,
        &session,
    )
    .expect_err("strike");
    assert!(error.to_string().contains("strike does not match"));

    let error = validate_record(
        instant(14, 30, 0, 0),
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Put,
        &contract,
        &session,
    )
    .expect_err("type");
    assert!(error.to_string().contains("type does not match"));
}

#[test]
fn the_closing_print_at_session_close_is_inclusive() {
    let contract = contract();
    let session = session();
    trade_rows(&[trade(instant(21, 0, 0, 0))], &contract, &session)
        .expect("closing print belongs to the session");
    quote_rows(&[quote(instant(21, 0, 0, 0))], &contract, &session)
        .expect("closing quote belongs to the session");
}
