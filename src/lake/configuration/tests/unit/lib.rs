use std::collections::BTreeMap;

use super::*;

#[test]
fn explicit_market_lake_directory_is_used_as_is() {
    let root = std::path::Path::new("C:/example/config/market-lake");
    assert_eq!(market_lake_config_dir(Some(root), &BTreeMap::new()), root);
}

#[test]
fn repository_root_gets_the_config_segment_appended() {
    let root = std::path::Path::new("C:/example");
    assert_eq!(
        market_lake_config_dir(Some(root), &BTreeMap::new()),
        std::path::Path::new("C:/example/config/market-lake")
    );
}

#[test]
fn configured_root_overrides_the_fallback() {
    let mut environ = BTreeMap::new();
    environ.insert("LAKE_CONFIG_ROOT".to_string(), "D:/elsewhere".to_string());
    assert_eq!(
        market_lake_config_dir(None, &environ),
        std::path::Path::new("D:/elsewhere/config/market-lake")
    );
}

#[test]
fn committed_configuration_loads_and_validates() {
    let environ = BTreeMap::new();
    let directory = market_lake_config_dir(None, &environ);
    if !directory.join("settings.yaml").is_file() {
        return;
    }
    let settings = LakeSettings::load(None, &environ).expect("settings");
    settings.validate().expect("valid");
    assert_eq!(settings.provider.name, "alpaca");
    assert_eq!(settings.storage.parquet.compression, "zstd");
    let universes = UniverseSettings::load(None, &environ).expect("universes");
    assert!(universes.equities.soak.symbols.contains(&"SPY".to_string()));
    assert!(!universes.crypto.pairs.is_empty());
}
