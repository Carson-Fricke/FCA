use super::*;
use crate::time::Date;

#[test]
fn partition_path_uses_the_frozen_hive_layout() {
    let date = Date::from_ymd(2024, 1, 18).expect("date");
    let path = partition_path(date, AssetClass::Equity, DataType::Trade, "spy").expect("path");
    assert_eq!(
        path,
        "date=2024-01-18/asset_class=equity/data_type=trade/SPY.parquet"
    );
}

#[test]
fn partition_path_percent_encodes_crypto_pairs() {
    let date = Date::from_ymd(2024, 1, 18).expect("date");
    let path = partition_path(date, AssetClass::Crypto, DataType::Quote, "BTC/USD").expect("path");
    assert_eq!(
        path,
        "date=2024-01-18/asset_class=crypto/data_type=quote/BTC%2FUSD.parquet"
    );
}

#[test]
fn symbol_filename_rejects_path_traversal() {
    assert!(symbol_filename("..").is_err());
    assert!(symbol_filename(".").is_err());
    assert!(symbol_filename("").is_err());
    assert!(symbol_filename("a\\b").is_err());
    assert_eq!(symbol_filename("brk.b").expect("dotted"), "BRK.B.parquet");
}

#[test]
fn supported_datasets_are_the_six_pinned_ones() {
    assert_eq!(SUPPORTED_DATASETS.len(), 6);
    assert!(SUPPORTED_DATASETS.contains(&(AssetClass::Option, DataType::Quote)));
    assert!(!SUPPORTED_DATASETS.contains(&(AssetClass::Reference, DataType::Classification)));
}
