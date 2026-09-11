use super::*;
use serde_json::json;

#[test]
fn records_callbacks_in_dispatch_order() {
    let mut strategy =
        ReferenceStrategy::new(BTreeMap::from([("label".to_string(), json!("alpha"))]));
    assert_eq!(strategy.parameters().get("label"), Some(&json!("alpha")));
    strategy.dispatch_lifecycle("on_start");
    strategy.dispatch("on_quote", r#"{"symbol":"SPY"}"#);
    strategy.dispatch("on_trade", r#"{"symbol":"SPY"}"#);
    strategy.dispatch_lifecycle("on_stop");
    assert_eq!(
        strategy.callbacks(),
        ["on_start", "on_quote", "on_trade", "on_stop"]
    );
    assert_eq!(strategy.events().len(), 2);
    assert_eq!(
        strategy.callbacks_json(),
        json!(["on_start", "on_quote", "on_trade", "on_stop"])
    );
}
