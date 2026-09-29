use super::*;
use lake_catalog::OptionType;
use lake_contracts::Date;
use serde_json::json;

fn expect_protocol(result: Result<impl std::fmt::Debug, ProviderError>, needle: &str) {
    match result {
        Err(ProviderError::Protocol(message)) => {
            assert!(
                message.contains(needle),
                "expected {needle:?} in protocol error, got {message:?}"
            );
        }
        other => panic!("expected protocol error containing {needle:?}, got {other:?}"),
    }
}

fn equity_trade_row() -> Value {
    json!({
        "t": "2024-11-27T14:30:00.123456Z",
        "i": 12345,
        "x": "V",
        "p": "189.425",
        "s": 100,
        "c": ["@", "I"],
        "z": "C",
        "u": "u1",
    })
}

fn equity_quote_row() -> Value {
    json!({
        "t": "2024-11-27T14:30:00Z",
        "bx": "V",
        "bp": 189.1,
        "bs": 2,
        "ax": "Q",
        "ap": "189.2",
        "as": 3,
        "c": ["R"],
        "z": "C",
    })
}

fn crypto_trade_row() -> Value {
    json!({
        "t": "2024-11-27T14:30:00.000001Z",
        "i": 7,
        "x": "CBSE",
        "p": "42000.10",
        "s": "0.015",
        "tks": "B",
    })
}

fn crypto_quote_row() -> Value {
    json!({
        "t": "2024-11-27T14:30:00Z",
        "bx": "CBSE",
        "bp": "42000.10",
        "bs": "0.1",
        "ax": "CBSE",
        "ap": "42001",
        "as": 2,
    })
}

#[test]
fn equity_trade_parses_every_field() {
    let trade = parse_trade(&equity_trade_row(), "AAPL").expect("trade");
    assert_eq!(
        trade.timestamp,
        DateTime::parse_iso_utc("2024-11-27T14:30:00.123456Z").expect("instant")
    );
    assert_eq!(trade.symbol, "AAPL");
    assert_eq!(trade.trade_id, 12345);
    assert_eq!(trade.exchange, "V");
    assert_eq!(trade.price, Decimal18::parse("189.425").expect("price"));
    assert_eq!(trade.size, 100);
    assert_eq!(trade.conditions, vec!["@".to_string(), "I".to_string()]);
    assert_eq!(trade.tape, "C");
    assert_eq!(trade.update.as_deref(), Some("u1"));
}

#[test]
fn equity_trade_update_is_optional_and_never_empty() {
    let mut row = equity_trade_row();
    row.as_object_mut().expect("object").remove("u");
    assert_eq!(parse_trade(&row, "AAPL").expect("trade").update, None);

    let mut row = equity_trade_row();
    row["u"] = Value::Null;
    assert_eq!(parse_trade(&row, "AAPL").expect("trade").update, None);

    let mut row = equity_trade_row();
    row["u"] = json!("");
    expect_protocol(parse_trade(&row, "AAPL"), "trade update");

    let mut row = equity_trade_row();
    row["u"] = json!(5);
    expect_protocol(parse_trade(&row, "AAPL"), "trade update");
}

#[test]
fn equity_trade_range_and_type_errors_name_the_field() {
    let mut row = equity_trade_row();
    row["s"] = json!(u32::MAX as u64 + 1);
    expect_protocol(parse_trade(&row, "AAPL"), "'s'");

    let mut row = equity_trade_row();
    row["s"] = json!(-1);
    expect_protocol(parse_trade(&row, "AAPL"), "'s'");

    let mut row = equity_trade_row();
    row["i"] = json!(-5);
    expect_protocol(parse_trade(&row, "AAPL"), "'i'");

    let mut row = equity_trade_row();
    row["i"] = json!(1.5);
    expect_protocol(parse_trade(&row, "AAPL"), "'i'");

    let mut row = equity_trade_row();
    row["i"] = json!(true);
    expect_protocol(parse_trade(&row, "AAPL"), "'i'");

    let mut row = equity_trade_row();
    row.as_object_mut().expect("object").remove("i");
    expect_protocol(parse_trade(&row, "AAPL"), "missing 'i'");

    let mut row = equity_trade_row();
    row["x"] = json!("");
    expect_protocol(parse_trade(&row, "AAPL"), "'x'");

    let mut row = equity_trade_row();
    row["p"] = json!("not-a-number");
    expect_protocol(parse_trade(&row, "AAPL"), "'p'");

    let mut row = equity_trade_row();
    row["p"] = json!("NaN");
    expect_protocol(parse_trade(&row, "AAPL"), "finite");

    let mut row = equity_trade_row();
    row["c"] = json!("@");
    expect_protocol(parse_trade(&row, "AAPL"), "conditions must be an array");

    let mut row = equity_trade_row();
    row["c"] = json!(["@", 5]);
    expect_protocol(parse_trade(&row, "AAPL"), "conditions must contain strings");

    let mut row = equity_trade_row();
    row.as_object_mut().expect("object").remove("c");
    expect_protocol(parse_trade(&row, "AAPL"), "missing 'c'");
}

#[test]
fn timestamp_requires_a_timezone_and_iso_shape() {
    let mut row = equity_trade_row();
    row["t"] = json!("2024-11-27T14:30:00");
    expect_protocol(parse_trade(&row, "AAPL"), "timezone");

    let mut row = equity_trade_row();
    row["t"] = json!("yesterday");
    expect_protocol(parse_trade(&row, "AAPL"), "timezone");

    let mut row = equity_trade_row();
    row["t"] = json!("2024-11-27T14:30:00+00:00");
    assert!(parse_trade(&row, "AAPL").is_ok());

    let mut row = equity_trade_row();
    row["t"] = json!("2024-13-27T14:30:00Z");
    expect_protocol(parse_trade(&row, "AAPL"), "ISO-8601");
}

#[test]
fn equity_quote_parses_every_field() {
    let quote = parse_quote(&equity_quote_row(), "MSFT").expect("quote");
    assert_eq!(quote.symbol, "MSFT");
    assert_eq!(quote.bid_exchange, "V");
    assert_eq!(quote.bid_price, Decimal18::parse("189.1").expect("price"));
    assert_eq!(quote.bid_size, 2);
    assert_eq!(quote.ask_exchange, "Q");
    assert_eq!(quote.ask_price, Decimal18::parse("189.2").expect("price"));
    assert_eq!(quote.ask_size, 3);
    assert_eq!(quote.conditions, vec!["R".to_string()]);
    assert_eq!(quote.tape, "C");
}

#[test]
fn equity_quote_size_errors_name_the_field() {
    let mut row = equity_quote_row();
    row["bs"] = json!(u32::MAX as u64 + 1);
    expect_protocol(parse_quote(&row, "MSFT"), "'bs'");

    let mut row = equity_quote_row();
    row["as"] = json!("3");
    expect_protocol(parse_quote(&row, "MSFT"), "'as'");

    let mut row = equity_quote_row();
    row["ap"] = json!(null);
    expect_protocol(parse_quote(&row, "MSFT"), "'ap'");

    let mut row = equity_quote_row();
    row.as_object_mut().expect("object").remove("ap");
    expect_protocol(parse_quote(&row, "MSFT"), "missing 'ap'");
}

#[test]
fn crypto_trade_parses_optional_fields() {
    let trade = parse_crypto_trade(&crypto_trade_row(), "BTC/USD").expect("trade");
    assert_eq!(trade.symbol, "BTC/USD");
    assert_eq!(trade.trade_id, Some(7));
    assert_eq!(trade.exchange.as_deref(), Some("CBSE"));
    assert_eq!(trade.price, Decimal18::parse("42000.10").expect("price"));
    assert_eq!(trade.size, Decimal18::parse("0.015").expect("size"));
    assert_eq!(trade.taker_side.as_deref(), Some("B"));
}

#[test]
fn crypto_trade_optional_fields_accept_missing_and_null() {
    let row = json!({
        "t": "2024-11-27T14:30:00Z",
        "p": "42000.10",
        "s": "0.015",
    });
    let trade = parse_crypto_trade(&row, "BTC/USD").expect("trade");
    assert_eq!(trade.trade_id, None);
    assert_eq!(trade.exchange, None);
    assert_eq!(trade.taker_side, None);

    let row = json!({
        "t": "2024-11-27T14:30:00Z",
        "i": null,
        "x": null,
        "p": "1",
        "s": "2",
        "tks": null,
    });
    let trade = parse_crypto_trade(&row, "BTC/USD").expect("trade");
    assert_eq!(trade.trade_id, None);
    assert_eq!(trade.exchange, None);
    assert_eq!(trade.taker_side, None);
}

#[test]
fn crypto_trade_rejects_bad_optional_values() {
    let mut row = crypto_trade_row();
    row["i"] = json!(i64::MAX as u64 + 1);
    expect_protocol(parse_crypto_trade(&row, "BTC/USD"), "'i'");

    let mut row = crypto_trade_row();
    row["x"] = json!("");
    expect_protocol(parse_crypto_trade(&row, "BTC/USD"), "'x'");

    let mut row = crypto_trade_row();
    row["tks"] = json!(1);
    expect_protocol(parse_crypto_trade(&row, "BTC/USD"), "'tks'");

    let mut row = crypto_trade_row();
    row["s"] = json!("many");
    expect_protocol(parse_crypto_trade(&row, "BTC/USD"), "'s'");
}

#[test]
fn crypto_quote_parses_fractional_sizes() {
    let quote = parse_crypto_quote(&crypto_quote_row(), "ETH/USD").expect("quote");
    assert_eq!(quote.bid_exchange.as_deref(), Some("CBSE"));
    assert_eq!(
        quote.bid_price,
        Decimal18::parse("42000.10").expect("price")
    );
    assert_eq!(quote.bid_size, Decimal18::parse("0.1").expect("size"));
    assert_eq!(quote.ask_exchange.as_deref(), Some("CBSE"));
    assert_eq!(quote.ask_price, Decimal18::parse("42001").expect("price"));
    assert_eq!(quote.ask_size, Decimal18::parse("2").expect("size"));

    let row = json!({
        "t": "2024-11-27T14:30:00Z",
        "bp": "1",
        "bs": "2",
        "ap": "3",
        "as": "4",
    });
    let quote = parse_crypto_quote(&row, "ETH/USD").expect("quote");
    assert_eq!(quote.bid_exchange, None);
    assert_eq!(quote.ask_exchange, None);
}

#[test]
fn crypto_quote_rejects_non_numeric_prices() {
    let mut row = crypto_quote_row();
    row["bp"] = json!("Infinity");
    expect_protocol(parse_crypto_quote(&row, "ETH/USD"), "finite");

    let mut row = crypto_quote_row();
    row["as"] = json!([]);
    expect_protocol(parse_crypto_quote(&row, "ETH/USD"), "'as'");
}

#[test]
fn page_rows_reads_keyed_equity_pages() {
    let payload = json!({
        "trades": {"AAPL": [{"i": 1}], "MSFT": [{"i": 2}]},
        "next_page_token": "token-1",
    });
    let (rows, token) = page_rows(&payload, DataType::Trades, "AAPL").expect("page");
    assert_eq!(rows, vec![&json!({"i": 1})]);
    assert_eq!(token.as_deref(), Some("token-1"));
}

#[test]
fn page_rows_reads_flat_equity_and_crypto_pages() {
    let payload = json!({"quotes": [{"bp": "1"}, {"bp": "2"}], "next_page_token": null});
    let (rows, token) = page_rows(&payload, DataType::Quotes, "AAPL").expect("page");
    assert_eq!(rows.len(), 2);
    assert_eq!(token, None);

    let payload = json!({"trades": [{"p": "1"}]});
    let (rows, token) = page_rows(&payload, DataType::Trades, "BTC/USD").expect("page");
    assert_eq!(rows.len(), 1);
    assert_eq!(token, None);
}

#[test]
fn page_rows_treats_missing_symbols_and_null_bodies_as_empty() {
    let payload = json!({"trades": {"MSFT": [{"i": 2}]}});
    let (rows, token) = page_rows(&payload, DataType::Trades, "AAPL").expect("page");
    assert!(rows.is_empty());
    assert_eq!(token, None);

    let payload = json!({"trades": {"AAPL": null}});
    let (rows, _) = page_rows(&payload, DataType::Trades, "AAPL").expect("page");
    assert!(rows.is_empty());

    let payload = json!({"trades": null});
    let (rows, _) = page_rows(&payload, DataType::Trades, "AAPL").expect("page");
    assert!(rows.is_empty());
}

#[test]
fn page_rows_rejects_malformed_shapes() {
    expect_protocol(
        page_rows(&json!([1, 2]), DataType::Trades, "AAPL"),
        "body must be an object",
    );
    expect_protocol(
        page_rows(&json!({"quotes": []}), DataType::Trades, "AAPL"),
        "missing 'trades'",
    );
    expect_protocol(
        page_rows(&json!({"trades": 5}), DataType::Trades, "AAPL"),
        "rows must be an array",
    );
    expect_protocol(
        page_rows(&json!({"trades": ["oops"]}), DataType::Trades, "AAPL"),
        "row must be an object",
    );
    expect_protocol(
        page_rows(&json!({"trades": {"AAPL": 5}}), DataType::Trades, "AAPL"),
        "rows must be an array",
    );
    expect_protocol(
        page_rows(
            &json!({"trades": [], "next_page_token": 5}),
            DataType::Trades,
            "AAPL",
        ),
        "next_page_token",
    );
    expect_protocol(
        page_rows(
            &json!({"trades": [], "next_page_token": {"t": 1}}),
            DataType::Trades,
            "AAPL",
        ),
        "next_page_token",
    );
}

fn option_contract(symbol: &str, option_type: OptionType) -> ContractRecord {
    ContractRecord::new(
        symbol,
        "SPY",
        Date::from_ymd(2024, 1, 19).expect("date"),
        Decimal18::parse("500").expect("strike"),
        option_type,
        100,
        None,
        None,
    )
    .expect("contract")
}

fn option_trade_row() -> Value {
    json!({
        "t": "2024-01-19T14:31:00.123456Z",
        "x": "XOPR",
        "p": "2.50",
        "s": 3,
        "c": "I",
    })
}

fn option_quote_row() -> Value {
    json!({
        "t": "2024-01-19T14:32:00Z",
        "bx": "XOPR",
        "bp": "2.40",
        "bs": 4,
        "ax": "CBOE",
        "ap": "2.60",
        "as": 5,
        "c": "A",
    })
}

#[test]
fn option_trade_parses_opra_fields_and_contract_identity() {
    let contract = option_contract("SPY240119C00500000", OptionType::Call);
    let trade = parse_option_trade(&option_trade_row(), &contract).expect("trade");
    assert_eq!(trade.timestamp.to_nanos(), 1705674660123456000);
    assert_eq!(trade.symbol, "SPY240119C00500000");
    assert_eq!(trade.underlying, "SPY");
    assert_eq!(
        trade.expiration_date,
        Date::from_ymd(2024, 1, 19).expect("d")
    );
    assert_eq!(trade.strike_price, Decimal18::parse("500").expect("strike"));
    assert_eq!(trade.option_type, OptionType::Call);
    assert_eq!(trade.exchange, "XOPR");
    assert_eq!(trade.price, Decimal18::parse("2.50").expect("price"));
    assert_eq!(trade.size, 3);
    assert_eq!(trade.condition, "I");
}

#[test]
fn option_quote_parses_bid_ask_and_single_condition() {
    let contract = option_contract("SPY240119P00500000", OptionType::Put);
    let quote = parse_option_quote(&option_quote_row(), &contract).expect("quote");
    assert_eq!(quote.option_type, OptionType::Put);
    assert_eq!(quote.bid_exchange, "XOPR");
    assert_eq!(quote.bid_size, 4);
    assert_eq!(quote.ask_exchange, "CBOE");
    assert_eq!(quote.ask_size, 5);
    assert_eq!(quote.condition, "A");
}

#[test]
fn option_rows_reject_missing_and_malformed_fields() {
    let contract = option_contract("SPY240119C00500000", OptionType::Call);
    let mut row = option_trade_row();
    row.as_object_mut().expect("object").remove("x");
    expect_protocol(parse_option_trade(&row, &contract), "missing 'x'");

    let mut row = option_trade_row();
    row["p"] = json!("not-a-number");
    expect_protocol(parse_option_trade(&row, &contract), "'p' must be numeric");

    let mut row = option_quote_row();
    row["bs"] = json!(-1);
    expect_protocol(
        parse_option_quote(&row, &contract),
        "'bs' must be an integer",
    );

    let mut row = option_trade_row();
    row["c"] = json!(["I"]);
    expect_protocol(
        parse_option_trade(&row, &contract),
        "'c' must be a non-empty string",
    );

    let mut row = option_trade_row();
    row["t"] = "2024-01-19T14:31:00".into();
    expect_protocol(
        parse_option_trade(&row, &contract),
        "must include a timezone",
    );
}

#[test]
fn option_page_rows_reject_unknown_symbols_and_preserve_contract_order() {
    let first = option_contract("SPY240119C00500000", OptionType::Call);
    let second = option_contract("SPY240119C00510000", OptionType::Call);
    let contracts = vec![
        (first.symbol.clone(), first.clone()),
        (second.symbol.clone(), second.clone()),
    ];

    // Keys are canonicalized (trimmed, uppercased) before the selection check,
    // and rows are returned in contract order rather than provider key order.
    let payload = json!({
        "trades": {
            "spy240119c00510000": [option_trade_row()],
            "  SPY240119C00500000 ": [option_trade_row()],
        },
        "next_page_token": null,
    });
    let (rows, token) = option_page_rows(&payload, DataType::Trades, &contracts).expect("rows");
    assert_eq!(token, None);
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].0.symbol, "SPY240119C00500000");
    assert_eq!(rows[1].0.symbol, "SPY240119C00510000");
}

#[test]
fn option_page_rows_rejects_malformed_pages() {
    let contract = option_contract("SPY240119C00500000", OptionType::Call);
    let contracts = vec![(contract.symbol.clone(), contract)];

    expect_protocol(
        option_page_rows(&json!([]), DataType::Trades, &contracts),
        "Alpaca option response body must be an object",
    );
    expect_protocol(
        option_page_rows(&json!({}), DataType::Trades, &contracts),
        "Alpaca option response is missing 'trades'",
    );
    expect_protocol(
        option_page_rows(&json!({"trades": []}), DataType::Trades, &contracts),
        "Alpaca option trades rows must be an object",
    );
    expect_protocol(
        option_page_rows(
            &json!({"trades": {"QQQ240119C00500000": []}}),
            DataType::Trades,
            &contracts,
        ),
        "unselected symbol",
    );
    expect_protocol(
        option_page_rows(
            &json!({"trades": {"SPY240119C00500000": {}}}),
            DataType::Trades,
            &contracts,
        ),
        "must be an array",
    );
    expect_protocol(
        option_page_rows(
            &json!({"trades": {"SPY240119C00500000": ["row"]}}),
            DataType::Trades,
            &contracts,
        ),
        "Alpaca option row must be an object",
    );
    expect_protocol(
        option_page_rows(
            &json!({"trades": {"SPY240119C00500000": []}, "next_page_token": 3}),
            DataType::Trades,
            &contracts,
        ),
        "next_page_token must be a string or null",
    );
}

#[test]
fn option_page_rows_returns_contract_order_without_their_rows() {
    let first = option_contract("SPY240119C00500000", OptionType::Call);
    let second = option_contract("SPY240119C00510000", OptionType::Call);
    let contracts = vec![
        (first.symbol.clone(), first),
        (second.symbol.clone(), second),
    ];
    let payload = json!({"trades": {"SPY240119C00510000": [option_trade_row()]}});
    let (rows, token) = option_page_rows(&payload, DataType::Trades, &contracts).expect("rows");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].0.symbol, "SPY240119C00510000");
    assert_eq!(token, None);
}

#[test]
fn parse_bar_reads_exact_decimals_from_a_recorded_equity_page() {
    let payload: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/alpaca_equity_bars_page1.json"))
            .expect("fixture");
    let (rows, token) = page_rows(&payload, DataType::Bars, "AAPL").expect("page");
    assert_eq!(token.as_deref(), Some("bars-page-2"));
    assert_eq!(rows.len(), 2);

    let bar = parse_bar(rows[0], "AAPL").expect("bar");
    assert_eq!(bar.symbol, "AAPL");
    assert_eq!(
        bar.timestamp,
        DateTime::from_ymd_hms(2024, 11, 27, 14, 30, 0, 0).expect("timestamp")
    );
    assert_eq!(bar.open, Decimal18::parse("190.12").expect("open"));
    assert_eq!(bar.high, Decimal18::parse("190.55").expect("high"));
    assert_eq!(bar.low, Decimal18::parse("189.98").expect("low"));
    assert_eq!(bar.close, Decimal18::parse("190.41").expect("close"));
    assert_eq!(bar.volume, Decimal18::parse("1200").expect("volume"));
}

#[test]
fn parse_bar_reads_fractional_crypto_volume() {
    let payload: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/alpaca_crypto_bars_page.json"))
            .expect("fixture");
    let (rows, token) = page_rows(&payload, DataType::Bars, "BTC/USD").expect("page");
    assert_eq!(token, None);
    let bar = parse_bar(rows[1], "BTC/USD").expect("bar");
    assert_eq!(bar.volume, Decimal18::parse("0.25").expect("volume"));
    assert_eq!(bar.open, Decimal18::parse("41250.00").expect("open"));
}

#[test]
fn parse_bar_rejects_missing_and_malformed_fields() {
    let row = json!({"t": "2024-11-27T14:30:00Z", "o": "1", "h": "2", "l": "0.5", "c": "1.5"});
    expect_protocol(parse_bar(&row, "AAPL"), "'v'");

    let row =
        json!({"t": "2024-11-27T14:30:00Z", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": []});
    expect_protocol(parse_bar(&row, "AAPL"), "'v'");

    let row =
        json!({"t": "2024-11-27T14:30:00", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "1"});
    expect_protocol(parse_bar(&row, "AAPL"), "timezone");
}
