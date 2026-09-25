use super::*;
use lake_catalog::ContractRecord;
use serde_json::json;

fn instant() -> DateTime {
    DateTime::from_ymd_hms(2024, 11, 27, 14, 30, 0, 123_456).expect("instant")
}

fn keys(value: &Value) -> Vec<String> {
    let mut keys: Vec<String> = value.as_object().expect("object").keys().cloned().collect();
    keys.sort();
    keys
}

#[test]
fn trade_row_has_exact_keys_and_values() {
    let trade = NormalizedTrade {
        timestamp: instant(),
        symbol: "AAPL".to_string(),
        trade_id: 9,
        exchange: "V".to_string(),
        price: Decimal18::parse("189.4200").expect("price"),
        size: 100,
        conditions: vec!["@".to_string(), "I".to_string()],
        tape: "C".to_string(),
        update: Some("u1".to_string()),
    };
    assert_eq!(
        keys(&trade.to_row()),
        vec![
            "conditions",
            "exchange",
            "price",
            "size",
            "symbol",
            "tape",
            "timestamp",
            "trade_id",
            "update",
        ]
    );
    assert_eq!(
        trade.to_row(),
        json!({
            "timestamp": instant().to_nanos(),
            "symbol": "AAPL",
            "trade_id": 9,
            "exchange": "V",
            "price": "189.42",
            "size": 100,
            "conditions": ["@", "I"],
            "tape": "C",
            "update": "u1",
        })
    );
}

#[test]
fn trade_row_renders_absent_update_as_null() {
    let trade = NormalizedTrade {
        timestamp: instant(),
        symbol: "AAPL".to_string(),
        trade_id: 9,
        exchange: "V".to_string(),
        price: Decimal18::ZERO,
        size: 1,
        conditions: Vec::new(),
        tape: "C".to_string(),
        update: None,
    };
    assert_eq!(trade.to_row()["update"], Value::Null);
    assert_eq!(trade.to_row()["price"], "0");
    assert_eq!(trade.to_row()["conditions"], json!([]));
}

#[test]
fn quote_row_has_exact_keys_and_values() {
    let quote = NormalizedQuote {
        timestamp: instant(),
        symbol: "MSFT".to_string(),
        bid_exchange: "V".to_string(),
        bid_price: Decimal18::parse("189.10").expect("price"),
        bid_size: 2,
        ask_exchange: "Q".to_string(),
        ask_price: Decimal18::parse("189.2").expect("price"),
        ask_size: 3,
        conditions: vec!["R".to_string()],
        tape: "C".to_string(),
    };
    assert_eq!(
        keys(&quote.to_row()),
        vec![
            "ask_exchange",
            "ask_price",
            "ask_size",
            "bid_exchange",
            "bid_price",
            "bid_size",
            "conditions",
            "symbol",
            "tape",
            "timestamp",
        ]
    );
    assert_eq!(
        quote.to_row(),
        json!({
            "timestamp": instant().to_nanos(),
            "symbol": "MSFT",
            "bid_exchange": "V",
            "bid_price": "189.1",
            "bid_size": 2,
            "ask_exchange": "Q",
            "ask_price": "189.2",
            "ask_size": 3,
            "conditions": ["R"],
            "tape": "C",
        })
    );
}

#[test]
fn crypto_trade_row_has_exact_keys_and_values() {
    let trade = NormalizedCryptoTrade {
        timestamp: instant(),
        symbol: "BTC/USD".to_string(),
        trade_id: Some(7),
        exchange: Some("CBSE".to_string()),
        price: Decimal18::parse("42000.10").expect("price"),
        size: Decimal18::parse("0.015").expect("size"),
        taker_side: Some("B".to_string()),
    };
    assert_eq!(
        keys(&trade.to_row()),
        vec![
            "exchange",
            "price",
            "size",
            "symbol",
            "taker_side",
            "timestamp",
            "trade_id",
        ]
    );
    assert_eq!(
        trade.to_row(),
        json!({
            "timestamp": instant().to_nanos(),
            "symbol": "BTC/USD",
            "trade_id": 7,
            "exchange": "CBSE",
            "price": "42000.1",
            "size": "0.015",
            "taker_side": "B",
        })
    );

    let empty = NormalizedCryptoTrade {
        timestamp: instant(),
        symbol: "BTC/USD".to_string(),
        trade_id: None,
        exchange: None,
        price: Decimal18::ONE,
        size: Decimal18::ONE,
        taker_side: None,
    };
    let row = empty.to_row();
    assert_eq!(row["trade_id"], Value::Null);
    assert_eq!(row["exchange"], Value::Null);
    assert_eq!(row["taker_side"], Value::Null);
}

#[test]
fn crypto_quote_row_has_exact_keys_and_values() {
    let quote = NormalizedCryptoQuote {
        timestamp: instant(),
        symbol: "ETH/USD".to_string(),
        bid_exchange: Some("CBSE".to_string()),
        bid_price: Decimal18::parse("42000.10").expect("price"),
        bid_size: Decimal18::parse("0.1").expect("size"),
        ask_exchange: None,
        ask_price: Decimal18::parse("42001").expect("price"),
        ask_size: Decimal18::parse("2").expect("size"),
    };
    assert_eq!(
        keys(&quote.to_row()),
        vec![
            "ask_exchange",
            "ask_price",
            "ask_size",
            "bid_exchange",
            "bid_price",
            "bid_size",
            "symbol",
            "timestamp",
        ]
    );
    assert_eq!(
        quote.to_row(),
        json!({
            "timestamp": instant().to_nanos(),
            "symbol": "ETH/USD",
            "bid_exchange": "CBSE",
            "bid_price": "42000.1",
            "bid_size": "0.1",
            "ask_exchange": null,
            "ask_price": "42001",
            "ask_size": "2",
        })
    );
}

fn option_contract(symbol: &str) -> ContractRecord {
    ContractRecord::new(
        symbol,
        "SPY",
        Date::from_ymd(2024, 1, 19).expect("date"),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        100,
        None,
        None,
    )
    .expect("contract")
}

#[test]
fn option_trade_row_has_exact_option_schema_keys_and_values() {
    let trade = NormalizedOptionTrade {
        timestamp: instant(),
        symbol: "SPY240119C00500000".to_string(),
        underlying: "SPY".to_string(),
        expiration_date: Date::from_ymd(2024, 1, 19).expect("date"),
        strike_price: Decimal18::parse("500").expect("strike"),
        option_type: OptionType::Call,
        exchange: "XOPR".to_string(),
        price: Decimal18::parse("2.50").expect("price"),
        size: 3,
        condition: "I".to_string(),
    };
    assert_eq!(
        trade.to_row(),
        json!({
            "timestamp": 1732717800123456000i64,
            "symbol": "SPY240119C00500000",
            "exchange": "XOPR",
            "price": "2.5",
            "size": 3,
            "condition": "I",
        })
    );
}

#[test]
fn option_quote_row_has_exact_option_schema_keys_and_values() {
    let quote = NormalizedOptionQuote {
        timestamp: instant(),
        symbol: "SPY240119C00500000".to_string(),
        underlying: "SPY".to_string(),
        expiration_date: Date::from_ymd(2024, 1, 19).expect("date"),
        strike_price: option_contract("SPY240119C00500000").strike_price,
        option_type: OptionType::Call,
        bid_exchange: "XOPR".to_string(),
        bid_price: Decimal18::parse("2.40").expect("bid"),
        bid_size: 4,
        ask_exchange: "CBOE".to_string(),
        ask_price: Decimal18::parse("2.60").expect("ask"),
        ask_size: 5,
        condition: "A".to_string(),
    };
    assert_eq!(
        quote.to_row(),
        json!({
            "timestamp": 1732717800123456000i64,
            "symbol": "SPY240119C00500000",
            "bid_exchange": "XOPR",
            "bid_price": "2.4",
            "bid_size": 4,
            "ask_exchange": "CBOE",
            "ask_price": "2.6",
            "ask_size": 5,
            "condition": "A",
        })
    );
}
