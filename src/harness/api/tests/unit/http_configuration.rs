use super::*;
use serde_json::json;

fn parse(body: Value) -> Result<ValidatedConfiguration, ConfigurationError> {
    StrictJsonConfigurationParser.parse(body.to_string().as_bytes())
}

#[test]
fn parses_a_backtest_document() {
    let configuration = parse(json!({
        "schema_version": 1,
        "shell": "backtest",
        "run_id": "run-1",
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-19T21:00:00Z",
        "strategy_instances": [
            {"instance_id": "z"},
            {"instance_id": "a"}
        ]
    }))
    .expect("parse");
    assert_eq!(configuration.run_id, "run-1");
    assert_eq!(configuration.strategy_ids, vec!["a", "z"]);
    assert_eq!(
        configuration.data_start,
        NaiveDate::from_ymd_opt(2024, 1, 2)
    );
    assert_eq!(configuration.data_end, NaiveDate::from_ymd_opt(2024, 1, 19));
    assert_eq!(configuration.configuration_digest.len(), 64);
}

#[test]
fn rejects_wrong_shell_and_unknown_fields() {
    assert!(parse(json!({"shell": "trader"})).is_err());
    assert!(parse(json!({"shell": "backtest", "run_id": "r", "unexpected": true})).is_err());
    assert!(parse(json!([1, 2, 3])).is_err());
}

#[test]
fn hash_is_stable() {
    let document = json!({"shell": "backtest", "run_id": "r"});
    assert_eq!(configuration_hash(&document), configuration_hash(&document));
}
