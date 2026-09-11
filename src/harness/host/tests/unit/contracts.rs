use super::*;

#[test]
fn callback_order_is_fixed() {
    assert!(validate_callback_order(&CALLBACK_ORDER).is_ok());
    assert!(validate_callback_order(&["on_start", "on_stop"]).is_err());
}

#[test]
fn event_kinds_map_to_callbacks() {
    assert_eq!(callback_name_for_event("quote"), "on_quote");
    assert_eq!(callback_name_for_event("fill"), "on_fill");
    assert_eq!(callback_name_for_event("mystery"), "on_event");
}

#[test]
fn missing_callbacks_are_rejected() {
    let mut present = BTreeSet::new();
    present.insert("on_start".to_string());
    let error = require_callbacks(&present, &["on_start", "on_stop"]).expect_err("error");
    assert_eq!(error.kind, ContractErrorKind::Type);
    assert!(error.message.contains("on_stop"));
}
