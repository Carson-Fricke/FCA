use super::*;
use lake_contracts::DateTime;

fn session() -> CoverageSession {
    CoverageSession {
        session_date: Date::from_ymd(2024, 1, 19).expect("date"),
        open_time: DateTime::from_ymd_hms(2024, 1, 19, 14, 30, 0, 0).expect("open"),
        close_time: DateTime::from_ymd_hms(2024, 1, 19, 21, 0, 0, 0).expect("close"),
        is_partial: false,
    }
}

fn at(hour: u32, minute: u32, second: u32, micros: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, hour, minute, second, micros).expect("instant")
}

fn trade(timestamp: DateTime, trade_id: u64) -> NormalizedTrade {
    NormalizedTrade {
        timestamp,
        symbol: "AAPL".to_string(),
        trade_id,
        exchange: "XNAS".to_string(),
        price: lake_contracts::Decimal18::parse("190.125").expect("price"),
        size: 100,
        conditions: vec!["@".to_string()],
        tape: "C".to_string(),
        update: None,
    }
}

fn quote(timestamp: DateTime) -> NormalizedQuote {
    NormalizedQuote {
        timestamp,
        symbol: "AAPL".to_string(),
        bid_exchange: "XNAS".to_string(),
        bid_price: lake_contracts::Decimal18::parse("190.12").expect("bid"),
        bid_size: 100,
        ask_exchange: "XNAS".to_string(),
        ask_price: lake_contracts::Decimal18::parse("190.13").expect("ask"),
        ask_size: 200,
        conditions: Vec::new(),
        tape: "C".to_string(),
    }
}

#[test]
fn trades_compare_on_every_pinned_tie_breaker() {
    let base = trade(at(14, 30, 0, 0), 1);
    assert_eq!(compare_trades(&base, &base), Ordering::Equal);

    let later = trade(at(14, 31, 0, 0), 1);
    assert_eq!(compare_trades(&base, &later), Ordering::Less);

    let higher_id = trade(at(14, 30, 0, 0), 2);
    assert_eq!(compare_trades(&base, &higher_id), Ordering::Less);

    let mut other_exchange = trade(at(14, 30, 0, 0), 1);
    other_exchange.exchange = "ARCX".to_string();
    assert_eq!(compare_trades(&other_exchange, &base), Ordering::Less);

    let mut cheaper = trade(at(14, 30, 0, 0), 1);
    cheaper.price = lake_contracts::Decimal18::parse("1").expect("price");
    assert_eq!(compare_trades(&cheaper, &base), Ordering::Less);

    let mut smaller = trade(at(14, 30, 0, 0), 1);
    smaller.size = 1;
    assert_eq!(compare_trades(&smaller, &base), Ordering::Less);

    let mut longer_conditions = trade(at(14, 30, 0, 0), 1);
    longer_conditions.conditions = vec!["@".to_string(), "F".to_string()];
    assert_eq!(compare_trades(&base, &longer_conditions), Ordering::Less);

    let mut other_tape = trade(at(14, 30, 0, 0), 1);
    other_tape.tape = "A".to_string();
    assert_eq!(compare_trades(&other_tape, &base), Ordering::Less);

    let mut updated = trade(at(14, 30, 0, 0), 1);
    updated.update = Some("T".to_string());
    assert_eq!(compare_trades(&base, &updated), Ordering::Less);
}

#[test]
fn quotes_compare_on_every_pinned_tie_breaker() {
    let base = quote(at(14, 30, 0, 0));
    assert_eq!(compare_quotes(&base, &base), Ordering::Equal);

    let later = quote(at(14, 31, 0, 0));
    assert_eq!(compare_quotes(&base, &later), Ordering::Less);

    let mut other_exchange = quote(at(14, 30, 0, 0));
    other_exchange.bid_exchange = "ARCX".to_string();
    assert_eq!(compare_quotes(&other_exchange, &base), Ordering::Less);

    let mut cheaper_bid = quote(at(14, 30, 0, 0));
    cheaper_bid.bid_price = lake_contracts::Decimal18::parse("1").expect("bid");
    assert_eq!(compare_quotes(&cheaper_bid, &base), Ordering::Less);

    let mut smaller_bid = quote(at(14, 30, 0, 0));
    smaller_bid.bid_size = 1;
    assert_eq!(compare_quotes(&smaller_bid, &base), Ordering::Less);

    let mut other_ask_exchange = quote(at(14, 30, 0, 0));
    other_ask_exchange.ask_exchange = "ARCX".to_string();
    assert_eq!(compare_quotes(&other_ask_exchange, &base), Ordering::Less);

    let mut cheaper_ask = quote(at(14, 30, 0, 0));
    cheaper_ask.ask_price = lake_contracts::Decimal18::parse("1").expect("ask");
    assert_eq!(compare_quotes(&cheaper_ask, &base), Ordering::Less);

    let mut smaller_ask = quote(at(14, 30, 0, 0));
    smaller_ask.ask_size = 1;
    assert_eq!(compare_quotes(&smaller_ask, &base), Ordering::Less);

    let mut with_conditions = quote(at(14, 30, 0, 0));
    with_conditions.conditions = vec!["R".to_string()];
    assert_eq!(compare_quotes(&base, &with_conditions), Ordering::Less);

    let mut tape = quote(at(14, 30, 0, 0));
    tape.tape = "A".to_string();
    assert_eq!(compare_quotes(&tape, &base), Ordering::Less);
}

#[test]
fn session_bounds_are_inclusive_for_record_validation() {
    let session = session();
    assert!(validate_record_timestamp(session.open_time, "AAPL", &session).is_ok());
    assert!(validate_record_timestamp(session.close_time, "AAPL", &session).is_ok());

    let after_close = at(21, 0, 0, 500_000);
    let error = validate_record_timestamp(after_close, "AAPL", &session).expect_err("after close");
    assert_eq!(
        error,
        DataError::value("AAPL record timestamp is outside session 2024-01-19")
    );
}

#[test]
fn symbol_mismatch_is_rejected_before_sorting() {
    let session = session();
    let mut wrong = trade(at(14, 30, 0, 0), 1);
    wrong.symbol = "MSFT".to_string();
    let error = validate_trade(&wrong, "AAPL", &session).expect_err("mismatch");
    assert_eq!(
        error,
        DataError::value("trade symbol does not match the requested symbol")
    );

    let mut wrong_quote = quote(at(14, 30, 0, 0));
    wrong_quote.symbol = " msft ".to_string();
    let error = validate_quote(&wrong_quote, "AAPL", &session).expect_err("mismatch");
    assert_eq!(
        error,
        DataError::value("quote symbol does not match the requested symbol")
    );
}

#[test]
fn coverage_status_prefers_partial_then_rows() {
    assert_eq!(coverage_status(true, 5), CoverageStatus::Partial);
    assert_eq!(coverage_status(true, 0), CoverageStatus::Partial);
    assert_eq!(coverage_status(false, 5), CoverageStatus::Complete);
    assert_eq!(coverage_status(false, 0), CoverageStatus::Empty);
}
