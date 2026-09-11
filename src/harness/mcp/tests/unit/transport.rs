use super::*;

#[test]
fn decode_rejects_malformed_json() {
    assert!(decode_message("not json").is_err());
    assert!(decode_message("{\"jsonrpc\":\"2.0\"}").is_ok());
}

#[test]
fn encode_is_single_line() {
    let encoded = encode_message(&json!({"a": 1}));
    assert!(!encoded.contains('\n'));
}
