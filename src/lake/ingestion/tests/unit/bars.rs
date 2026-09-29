use super::*;

use serde_json::Value;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(text: &str) -> DateTime {
    DateTime::parse_iso_utc(text).expect("valid instant")
}

fn coverage_session(value: &Value) -> CoverageSession {
    CoverageSession::new(
        Date::parse(value["date"].as_str().expect("date")).expect("valid date"),
        instant(value["open"].as_str().expect("open")),
        instant(value["close"].as_str().expect("close")),
        false,
    )
    .expect("valid session")
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_str().expect("string").to_string())
        .collect()
}

fn equity_trade(value: &Value) -> EquityTradeRecord {
    EquityTradeRecord {
        timestamp: instant(value["timestamp"].as_str().expect("timestamp")),
        symbol: value["symbol"].as_str().expect("symbol").to_string(),
        trade_id: value["trade_id"].as_u64().expect("trade id"),
        exchange: value["exchange"].as_str().expect("exchange").to_string(),
        price: Decimal18::parse(value["price"].as_str().expect("price")).expect("price"),
        size: u32::try_from(value["size"].as_u64().expect("size")).expect("size"),
        conditions: strings(&value["conditions"]),
        tape: value["tape"].as_str().expect("tape").to_string(),
        update: value
            .get("update")
            .and_then(Value::as_str)
            .map(str::to_string),
    }
}

fn crypto_trade(value: &Value) -> CryptoTradeRecord {
    CryptoTradeRecord {
        timestamp: instant(value["timestamp"].as_str().expect("timestamp")),
        symbol: value["symbol"].as_str().expect("symbol").to_string(),
        trade_id: value.get("trade_id").and_then(Value::as_i64),
        exchange: value
            .get("exchange")
            .and_then(Value::as_str)
            .map(str::to_string),
        price: Decimal18::parse(value["price"].as_str().expect("price")).expect("price"),
        size: Decimal18::parse(value["size"].as_str().expect("size")).expect("size"),
        taker_side: value
            .get("taker_side")
            .and_then(Value::as_str)
            .map(str::to_string),
    }
}

fn assert_canonical_rows(derived: &[NormalizedBar], expected: &Value) {
    let actual = Value::Array(derived.iter().map(NormalizedBar::to_row).collect());
    assert_eq!(
        serde_json::to_string(&actual).expect("actual rows"),
        serde_json::to_string(expected).expect("expected rows"),
        "derived bars must byte-match the pinned rows"
    );
}

fn equity_fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/equity_bars_derivation.json"))
        .expect("equity fixture")
}

fn crypto_fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/crypto_bars_derivation.json"))
        .expect("crypto fixture")
}

#[test]
fn equity_derivation_matches_the_pinned_minute_and_daily_bars() {
    let fixture = equity_fixture();
    let session = coverage_session(&fixture["session"]);
    let trades: Vec<EquityTradeRecord> = fixture["trades"]
        .as_array()
        .expect("trades")
        .iter()
        .map(equity_trade)
        .collect();

    let minute =
        derive_equity_bars("AAPL", &trades, BarInterval::OneMinute, &session).expect("minute bars");
    assert_canonical_rows(&minute, &fixture["minute_bars"]);

    let daily =
        derive_equity_bars("AAPL", &trades, BarInterval::OneDay, &session).expect("daily bars");
    assert_canonical_rows(&daily, &fixture["daily_bars"]);
}

#[test]
fn crypto_derivation_matches_the_pinned_minute_and_daily_bars() {
    let fixture = crypto_fixture();
    let session = coverage_session(&fixture["session"]);
    let trades: Vec<CryptoTradeRecord> = fixture["trades"]
        .as_array()
        .expect("trades")
        .iter()
        .map(crypto_trade)
        .collect();

    let minute = derive_crypto_bars("BTC/USD", &trades, BarInterval::OneMinute, &session)
        .expect("minute bars");
    assert_canonical_rows(&minute, &fixture["minute_bars"]);

    let daily =
        derive_crypto_bars("BTC/USD", &trades, BarInterval::OneDay, &session).expect("daily bars");
    assert_canonical_rows(&daily, &fixture["daily_bars"]);
}

#[test]
fn derivation_is_independent_of_arrival_order() {
    let fixture = equity_fixture();
    let session = coverage_session(&fixture["session"]);
    let trades: Vec<EquityTradeRecord> = fixture["trades"]
        .as_array()
        .expect("trades")
        .iter()
        .map(equity_trade)
        .collect();
    let mut reversed = trades.clone();
    reversed.reverse();

    for interval in [BarInterval::OneMinute, BarInterval::OneDay] {
        assert_eq!(
            derive_equity_bars("AAPL", &trades, interval, &session).expect("forward"),
            derive_equity_bars("AAPL", &reversed, interval, &session).expect("reversed")
        );
    }
}

#[test]
fn a_trade_at_the_session_close_lands_in_the_final_minute_window() {
    let fixture = equity_fixture();
    let session = coverage_session(&fixture["session"]);
    let trades: Vec<EquityTradeRecord> = fixture["trades"]
        .as_array()
        .expect("trades")
        .iter()
        .map(equity_trade)
        .collect();
    let minute =
        derive_equity_bars("AAPL", &trades, BarInterval::OneMinute, &session).expect("minute bars");

    let last = minute.last().expect("close bucket");
    assert_eq!(last.timestamp, instant("2024-11-27T20:59:00Z"));
    assert_eq!(last.close, Decimal18::parse("190").expect("close"));
    assert_eq!(last.volume, Decimal18::parse("1").expect("volume"));
}

#[test]
fn sessions_without_trades_derive_no_bars() {
    let session = CoverageSession::new(
        day(2024, 11, 27),
        instant("2024-11-27T14:30:00Z"),
        instant("2024-11-27T21:00:00Z"),
        false,
    )
    .expect("session");
    assert!(
        derive_equity_bars("AAPL", &[], BarInterval::OneMinute, &session)
            .expect("minute bars")
            .is_empty()
    );
    assert!(
        derive_crypto_bars("BTC/USD", &[], BarInterval::OneDay, &session)
            .expect("daily bars")
            .is_empty()
    );
}
