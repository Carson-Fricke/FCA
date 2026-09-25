use super::*;
use serde_json::json;

#[test]
fn encode_query_leaves_an_empty_parameter_list_alone() {
    assert_eq!(
        encode_query("https://example.test/v2/stocks/trades", &[]),
        "https://example.test/v2/stocks/trades"
    );
}

#[test]
fn encode_query_renders_python_compatible_percent_encoding() {
    let params = vec![
        (
            "start".to_string(),
            "2024-11-27T14:30:00.123456Z".to_string(),
        ),
        ("symbols".to_string(), "BTC/USD".to_string()),
        ("sort".to_string(), "asc".to_string()),
    ];
    assert_eq!(
        encode_query("https://example.test/v1beta3/crypto/us/trades", &params),
        concat!(
            "https://example.test/v1beta3/crypto/us/trades",
            "?start=2024-11-27T14%3A30%3A00.123456Z",
            "&symbols=BTC%2FUSD",
            "&sort=asc",
        )
    );
}

#[test]
fn decode_body_requires_json_on_success() {
    match decode_body("<html>oops</html>", 200) {
        Err(TransportFailure::Protocol(message)) => {
            assert!(message.contains("not JSON"), "{message}");
        }
        other => panic!("expected protocol failure, got {other:?}"),
    }
}

#[test]
fn decode_body_allows_non_json_error_pages() {
    assert_eq!(decode_body("<html>oops</html>", 503), Ok(Value::Null));
    assert_eq!(decode_body("", 200), Ok(Value::Null));
    assert_eq!(
        decode_body("{\"trades\": []}", 200),
        Ok(json!({"trades": []}))
    );
}

#[test]
fn reqwest_transport_construction_tolerates_bad_timeouts() {
    let _ = ReqwestTransport::new(0.0);
    let _ = ReqwestTransport::new(f64::NAN);
    let _ = ReqwestTransport::new(30.0);
}
