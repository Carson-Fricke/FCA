//! Strict option selectors survive the public submission configuration boundary.
use config::{models::build_backtest, parse_yaml_mapping, YamlValue};
use std::path::Path;

#[test]
fn both_option_selectors_are_validated_and_retained_in_configuration_hashes() {
    let text = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../config/harness/backtest.yaml"),
    )
    .unwrap();
    let base = parse_yaml_mapping(&text).unwrap();
    let baseline = build_backtest(&base).unwrap();
    for choice in [
        "expiration: '2024-02-16'\nstrike: '475'",
        "days_to_expiration: 30\ndelta: '0.50'",
    ] {
        let selection =
            format!("underlying: SPY\nright: call\nas_of: '2024-01-02T21:00:00Z'\n{choice}\n");
        let mut doc = base.clone();
        let YamlValue::Map(data) = doc.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "options".into(),
            YamlValue::Map(std::collections::BTreeMap::from([(
                "MSFT".into(),
                YamlValue::Map(parse_yaml_mapping(&selection).unwrap()),
            )])),
        );
        let canonical = build_backtest(&doc).unwrap();
        assert_eq!(canonical["data"]["options"]["MSFT"]["underlying"], "SPY");
        assert_eq!(
            canonical["data"]["options"]["MSFT"]["max_quote_age_seconds"],
            3600
        );
        assert_ne!(
            config::configuration_hash(&canonical),
            config::configuration_hash(&baseline)
        );
        let YamlValue::Map(data) = doc.get_mut("data").unwrap() else {
            panic!()
        };
        let YamlValue::Map(options) = data.get_mut("options").unwrap() else {
            panic!()
        };
        let value = options.remove("MSFT").unwrap();
        options.insert("UNDECLARED".into(), value);
        assert!(build_backtest(&doc).is_err());
    }
}
