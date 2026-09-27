use super::*;
use lake_contracts::Decimal18;

fn day() -> Date {
    Date::from_ymd(2024, 1, 18).expect("date")
}

fn session() -> CoverageSession {
    CoverageSession {
        session_date: day(),
        open_time: DateTime::midnight(day()),
        close_time: DateTime::midnight(day().add_days(1)),
        is_partial: false,
    }
}

fn at(hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 18, hour, minute, 0, 0).expect("instant")
}

fn trade(timestamp: DateTime) -> NormalizedCryptoTrade {
    NormalizedCryptoTrade {
        timestamp,
        symbol: "BTC/USD".to_string(),
        trade_id: Some(1),
        exchange: Some("CBSE".to_string()),
        price: Decimal18::parse("41234.12").expect("price"),
        size: Decimal18::parse("0.01").expect("size"),
        taker_side: Some("B".to_string()),
    }
}

#[test]
fn pairs_are_canonicalised_and_validated_like_the_python_regex() {
    assert_eq!(canonical_pair(" btc/usd ").expect("pair"), "BTC/USD");
    assert_eq!(canonical_pair("ETH/BTC").expect("pair"), "ETH/BTC");
    assert_eq!(canonical_pair("1INCH/USD").expect("pair"), "1INCH/USD");

    for invalid in ["BTC-USD", "BTC/USD/ETH", "/USD", "BTC/", "", "BTC USD"] {
        let error = canonical_pair(invalid).expect_err("invalid pair");
        assert_eq!(
            error,
            DataError::value(format!("invalid crypto pair: {invalid:?}"))
        );
    }
}

#[test]
fn sessions_must_be_exactly_one_utc_calendar_day() {
    assert!(validate_session(&session()).is_ok());

    let shifted = CoverageSession {
        session_date: day(),
        open_time: DateTime::from_micros(DateTime::midnight(day()).micros() + 1),
        close_time: DateTime::midnight(day().add_days(1)),
        is_partial: false,
    };
    assert_eq!(
        validate_session(&shifted).expect_err("shifted open"),
        DataError::value("crypto session must be one UTC calendar day")
    );

    let short = CoverageSession {
        session_date: day(),
        open_time: DateTime::midnight(day()),
        close_time: DateTime::from_micros(DateTime::midnight(day().add_days(1)).micros() - 1),
        is_partial: false,
    };
    assert_eq!(
        validate_session(&short).expect_err("short close"),
        DataError::value("crypto session must be one UTC calendar day")
    );
}

#[test]
fn crypto_trades_sort_with_missing_identifiers_first() {
    let mut without_id = trade(at(0, 1));
    without_id.trade_id = None;
    without_id.exchange = None;
    let base = trade(at(0, 1));

    assert_eq!(compare_trades(&without_id, &base), Ordering::Less);
    assert_eq!(compare_trades(&base, &without_id), Ordering::Greater);

    let mut earlier = trade(at(0, 0));
    earlier.trade_id = Some(99);
    assert_eq!(compare_trades(&earlier, &base), Ordering::Less);

    let mut other_exchange = trade(at(0, 1));
    other_exchange.exchange = Some("A".to_string());
    assert_eq!(compare_trades(&other_exchange, &base), Ordering::Less);

    let mut smaller = trade(at(0, 1));
    smaller.size = Decimal18::parse("0.001").expect("size");
    assert_eq!(compare_trades(&smaller, &base), Ordering::Less);

    let mut other_side = trade(at(0, 1));
    other_side.taker_side = None;
    assert_eq!(compare_trades(&other_side, &base), Ordering::Less);
}

#[test]
fn crypto_quotes_sort_with_missing_exchanges_first() {
    let mut quote = NormalizedCryptoQuote {
        timestamp: at(0, 1),
        symbol: "BTC/USD".to_string(),
        bid_exchange: Some("CBSE".to_string()),
        bid_price: Decimal18::parse("41234.12").expect("bid"),
        bid_size: Decimal18::parse("0.1").expect("bid size"),
        ask_exchange: Some("CBSE".to_string()),
        ask_price: Decimal18::parse("41235.12").expect("ask"),
        ask_size: Decimal18::parse("0.2").expect("ask size"),
    };
    let base = quote.clone();

    quote.bid_exchange = None;
    assert_eq!(compare_quotes(&quote, &base), Ordering::Less);

    let mut other_ask = base.clone();
    other_ask.ask_exchange = Some("A".to_string());
    assert_eq!(compare_quotes(&other_ask, &base), Ordering::Less);

    let mut cheaper = base.clone();
    cheaper.bid_price = Decimal18::parse("1").expect("bid");
    assert_eq!(compare_quotes(&cheaper, &base), Ordering::Less);

    let mut larger = base.clone();
    larger.ask_size = Decimal18::parse("9").expect("ask size");
    assert_eq!(compare_quotes(&base, &larger), Ordering::Less);
}

#[test]
fn crypto_record_bounds_end_at_the_inclusive_query_end() {
    let session = session();
    let query_end = at(12, 0);
    assert!(validate_record_timestamp(session.open_time, "BTC/USD", &session, query_end).is_ok());
    assert!(validate_record_timestamp(query_end, "BTC/USD", &session, query_end).is_ok());

    let after = DateTime::from_micros(query_end.micros() + 1);
    assert_eq!(
        validate_record_timestamp(after, "BTC/USD", &session, query_end).expect_err("after end"),
        DataError::value("BTC/USD record timestamp is outside UTC day 2024-01-18")
    );
}

#[test]
fn coverage_status_prefers_partial_then_rows() {
    assert_eq!(coverage_status(true, 0), CoverageStatus::Partial);
    assert_eq!(coverage_status(false, 3), CoverageStatus::Complete);
    assert_eq!(coverage_status(false, 0), CoverageStatus::Empty);
}
