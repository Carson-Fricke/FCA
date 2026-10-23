use harness_contracts::canonical_json;
use harness_contracts::canonical_subscriptions;
use harness_contracts::configuration_hash;
use harness_contracts::BarInterval;
use harness_contracts::DataType;
use harness_contracts::HashEncoding;
use harness_contracts::Subscription;
use serde_json::json;

#[test]
fn historical_v1_unicode_digests_are_explicit_and_immutable() {
    let document = json!({"z":"\u{1f600}","a":"caf\u{e9}"});
    assert_eq!(
        canonical_json(&document, HashEncoding::HistoricalAscii),
        r#"{"a":"caf\u00e9","z":"\ud83d\ude00"}"#
    );
    assert_eq!(
        configuration_hash(&document, HashEncoding::Utf8),
        "a636b16df0b3d69ad19d9cb5a6518a4c1201dd822eca43d499b532bca8a84aad"
    );
    assert_eq!(
        configuration_hash(&document, HashEncoding::HistoricalAscii),
        "cd1fb8a37b56147ef2c9ab771fd4c9dfb18ec2863a83663faeeab52dc899af65"
    );
    let reordered = json!({"a":"caf\u{e9}","z":"\u{1f600}"});
    assert_eq!(
        configuration_hash(&document, HashEncoding::Utf8),
        configuration_hash(&reordered, HashEncoding::Utf8)
    );
}

#[test]
fn domain_intervals_are_not_replay_policy_and_unknown_types_fail() {
    let input = Subscription::Bars {
        symbols: vec!["QQQ".into(), "SPY".into(), "QQQ".into()],
        interval: "5min".into(),
    };
    let normalized = canonical_subscriptions(vec![input]).unwrap();
    assert_eq!(normalized[0].symbols(), &["QQQ", "SPY"]);
    assert!(DataType::parse("ticks").is_err());
    let interval = serde_json::from_value::<BarInterval>(json!("5min")).unwrap();
    assert_eq!(serde_json::to_value(interval).unwrap(), json!("5min"));
    assert!(serde_json::from_value::<BarInterval>(json!("1m")).is_err());
    assert!(
        serde_json::from_value::<Subscription>(json!({"data_type":"ticks","symbols":["SPY"]}))
            .is_err()
    );
    assert!(serde_json::from_value::<Subscription>(
        json!({"data_type":"quote","symbols":["SPY"],"interval":null})
    )
    .is_err());
}
