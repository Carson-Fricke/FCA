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

#[test]
fn parses_a_screen_document_and_derives_the_run_id() {
    let source = "fn main() {}\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    let document = json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "close-screen",
        "as_of": "2024-01-19",
        "bar_interval": "1d",
        "lookback_days": 20,
        "rank_metric": "close",
        "universe_revision": "sp500-2024-01",
        "source": source,
        "source_sha256": digest,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "a".repeat(64)),
        "definition_hash": "b".repeat(64),
        "parameters": {"lookback": 20},
    });
    let configuration = parse(document.clone()).expect("parse");
    let screen = configuration.screen.as_ref().expect("typed screen config");
    assert_eq!(configuration.run_id, screen.run_id());
    assert!(configuration.run_id.starts_with("screen-"));
    assert_eq!(
        configuration.strategy_ids,
        vec![screen.definition_id.clone()]
    );
    assert_eq!(
        configuration.data_start,
        NaiveDate::from_ymd_opt(2023, 12, 30)
    );
    assert_eq!(configuration.data_end, NaiveDate::from_ymd_opt(2024, 1, 19));

    let mut unknown = document.clone();
    unknown["run_id"] = json!("not-allowed");
    assert!(parse(unknown).is_err());

    let mut mismatch = document;
    mismatch["bundle_hash"] = json!("c".repeat(64));
    assert!(parse(mismatch).is_err());
}
