use super::*;

#[test]
fn matches_python_dumps_style() {
    let value = JsonValue::object([
        ("b", JsonValue::Int(2)),
        ("a", JsonValue::String("x".to_string())),
        (
            "c",
            JsonValue::Array(vec![JsonValue::Bool(true), JsonValue::Null]),
        ),
    ]);
    assert_eq!(
        value.to_canonical_string(),
        r#"{"a":"x","b":2,"c":[true,null]}"#
    );
}

#[test]
fn escapes_control_and_non_ascii() {
    let value = JsonValue::String("a\"b\\c\nd\u{00e9}\u{1f600}".to_string());
    assert_eq!(
        value.to_canonical_string(),
        "\"a\\\"b\\\\c\\nd\\u00e9\\ud83d\\ude00\""
    );
}

#[test]
fn empty_containers() {
    assert_eq!(JsonValue::Array(vec![]).to_canonical_string(), "[]");
    assert_eq!(
        JsonValue::Object(BTreeMap::new()).to_canonical_string(),
        "{}"
    );
}

#[test]
fn utf8_variant_keeps_literal_unicode() {
    let value = JsonValue::String("caf\u{00e9}\u{1f600}".to_string());
    assert_eq!(value.to_canonical_string(), "\"caf\\u00e9\\ud83d\\ude00\"");
    assert_eq!(value.to_canonical_string_utf8(), "\"caf\u{00e9}\u{1f600}\"");
}
