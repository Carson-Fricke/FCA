use super::*;
use serde_json::json;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn valid_payload() -> Value {
    json!({
        "asset_class": "option",
        "data_types": ["trade", "quote"],
        "end_time": "2024-01-19T21:00:00Z",
        "expiration_date": "2024-01-19",
        "option_type": "call",
        "session_date": "2024-01-19",
        "start_time": "2024-01-19T14:30:00Z",
        "strike_price": "500",
        "symbol": "SPY240119C00500000",
        "underlying": "SPY",
    })
}

#[test]
fn contract_from_payload_decodes_the_osi_identity() {
    let contract = contract_from_payload(&valid_payload()).expect("contract");
    assert_eq!(contract.symbol, "SPY240119C00500000");
    assert_eq!(contract.underlying, "SPY");
    assert_eq!(contract.expiration_date, day(2024, 1, 19));
    assert_eq!(
        contract.strike_price,
        Decimal18::parse("500").expect("strike")
    );
    assert_eq!(contract.option_type, OptionType::Call);
    assert_eq!(contract.multiplier, 100);
    assert_eq!(contract.listed_date, None);
    assert_eq!(contract.delisted_date, None);
}

#[test]
fn contract_from_payload_rejects_bad_fields() {
    let mut payload = valid_payload();
    payload["strike_price"] = json!("not-a-decimal");
    let error = contract_from_payload(&payload).expect_err("strike");
    assert_eq!(error.to_string(), "strike_price must be a decimal number");

    let mut payload = valid_payload();
    payload["option_type"] = json!("straddle");
    let error = contract_from_payload(&payload).expect_err("type");
    assert_eq!(error.to_string(), "option_type must be 'call' or 'put'");

    for name in [
        "symbol",
        "underlying",
        "expiration_date",
        "strike_price",
        "option_type",
    ] {
        let mut payload = valid_payload();
        payload[name] = json!("   ");
        let error = contract_from_payload(&payload).expect_err(name);
        assert_eq!(
            error.to_string(),
            format!("option queue field '{name}' must be a non-empty string")
        );
        let mut payload = valid_payload();
        payload[name] = json!(3);
        let error = contract_from_payload(&payload).expect_err(name);
        assert_eq!(
            error.to_string(),
            format!("option queue field '{name}' must be a non-empty string")
        );
    }
}

#[test]
fn required_dates_and_datetimes_follow_the_python_shapes() {
    let payload = valid_payload();
    assert_eq!(
        required_date(&payload, "session_date").expect("date"),
        day(2024, 1, 19)
    );
    let mut payload = valid_payload();
    payload["session_date"] = json!("19/01/2024");
    let error = required_date(&payload, "session_date").expect_err("date");
    assert_eq!(
        error.to_string(),
        "option queue field 'session_date' must be an ISO date"
    );

    let parsed = required_datetime(&payload, "start_time").expect("start");
    assert_eq!(
        parsed,
        DateTime::from_ymd_hms(2024, 1, 19, 14, 30, 0, 0).expect("open")
    );
    let mut payload = valid_payload();
    payload["start_time"] = json!("2024-01-19T14:30:00");
    let error = required_datetime(&payload, "start_time").expect_err("naive");
    assert_eq!(
        error.to_string(),
        "option queue field 'start_time' must include a timezone"
    );
    let mut payload = valid_payload();
    payload["start_time"] = json!("2024-01-19T09:30:00-05:00");
    let parsed = required_datetime(&payload, "start_time").expect("offset");
    assert_eq!(
        parsed,
        DateTime::from_ymd_hms(2024, 1, 19, 14, 30, 0, 0).expect("open")
    );
}

#[test]
fn data_types_must_be_a_non_empty_string_array() {
    let payload = valid_payload();
    let parsed = payload_data_types(&payload).expect("types");
    assert_eq!(parsed, vec![DataType::Trade, DataType::Quote]);

    let mut payload = valid_payload();
    payload["data_types"] = json!([]);
    let error = payload_data_types(&payload).expect_err("empty");
    assert_eq!(
        error.to_string(),
        "option queue message data_types must be a non-empty array"
    );

    let mut payload = valid_payload();
    payload["data_types"] = json!(["trade", 3]);
    let error = payload_data_types(&payload).expect_err("non-string");
    assert_eq!(
        error.to_string(),
        "option queue message data_types must contain strings"
    );

    let mut payload = valid_payload();
    payload["data_types"] = json!(["trade", "gamma"]);
    let error = payload_data_types(&payload).expect_err("unknown");
    assert_eq!(error.to_string(), "\"gamma\" is not a valid DataType");
}
