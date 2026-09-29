use super::*;
use crate::time::Date;

#[test]
fn partition_path_uses_the_frozen_hive_layout() {
    let date = Date::from_ymd(2024, 1, 18).expect("date");
    let path =
        partition_path(date, AssetClass::Equity, DataType::Trade, None, "spy").expect("path");
    assert_eq!(
        path,
        "date=2024-01-18/asset_class=equity/data_type=trade/SPY.parquet"
    );
}

#[test]
fn partition_path_percent_encodes_crypto_pairs() {
    let date = Date::from_ymd(2024, 1, 18).expect("date");
    let path =
        partition_path(date, AssetClass::Crypto, DataType::Quote, None, "BTC/USD").expect("path");
    assert_eq!(
        path,
        "date=2024-01-18/asset_class=crypto/data_type=quote/BTC%2FUSD.parquet"
    );
}

#[test]
fn partition_path_places_the_interval_before_the_symbol_for_bars() {
    let date = Date::from_ymd(2024, 1, 18).expect("date");
    let minute = partition_path(
        date,
        AssetClass::Equity,
        DataType::Bars,
        Some("1min"),
        "spy",
    )
    .expect("bars path");
    assert_eq!(
        minute,
        "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/SPY.parquet"
    );
    let day = partition_path(
        date,
        AssetClass::Crypto,
        DataType::Bars,
        Some("1d"),
        "BTC/USD",
    )
    .expect("crypto bars path");
    assert_eq!(
        day,
        "date=2024-01-18/asset_class=crypto/data_type=bars/interval=1d/BTC%2FUSD.parquet"
    );
}

#[test]
fn partition_path_rejects_non_bars_intervals_and_missing_or_invalid_bar_intervals() {
    let date = Date::from_ymd(2024, 1, 18).expect("date");

    let error = partition_path(
        date,
        AssetClass::Equity,
        DataType::Trade,
        Some("1min"),
        "SPY",
    )
    .expect_err("trade with interval");
    assert_eq!(
        error,
        DataError::value("interval is supported for bars partitions only")
    );

    let error = partition_path(date, AssetClass::Equity, DataType::Bars, None, "SPY")
        .expect_err("bars without interval");
    assert_eq!(
        error,
        DataError::value("bars partitions require an interval token")
    );

    for token in ["1s", "1h", "5min", "1MIN", "", " 1min"] {
        let error = partition_path(date, AssetClass::Equity, DataType::Bars, Some(token), "SPY")
            .expect_err("invalid token");
        assert_eq!(
            error,
            DataError::value(format!(
                "invalid bar interval: {token:?}; expected one of 1min, 1d"
            ))
        );
    }
}

#[test]
fn bar_interval_validates_exactly_the_canonical_tokens() {
    assert_eq!(
        BarInterval::parse("1min").expect("minute"),
        BarInterval::OneMinute
    );
    assert_eq!(BarInterval::parse("1d").expect("day"), BarInterval::OneDay);
    assert_eq!(BarInterval::OneMinute.value(), "1min");
    assert_eq!(BarInterval::OneDay.value(), "1d");
    assert_eq!(
        BarInterval::from_value("1min"),
        Some(BarInterval::OneMinute)
    );
    assert_eq!(BarInterval::from_value("1d"), Some(BarInterval::OneDay));
    assert_eq!(BarInterval::from_value("1h"), None);
    assert_eq!(BarInterval::OneMinute.to_string(), "1min");
    assert_eq!(BarInterval::OneDay.to_string(), "1d");
    assert!(BarInterval::parse("1s").is_err());
    assert!(BarInterval::parse("1H").is_err());
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
fn supported_datasets_include_equity_and_crypto_bars_but_not_option_bars() {
    assert_eq!(SUPPORTED_DATASETS.len(), 8);
    assert!(SUPPORTED_DATASETS.contains(&(AssetClass::Option, DataType::Quote)));
    assert!(SUPPORTED_DATASETS.contains(&(AssetClass::Equity, DataType::Bars)));
    assert!(SUPPORTED_DATASETS.contains(&(AssetClass::Crypto, DataType::Bars)));
    assert!(!SUPPORTED_DATASETS.contains(&(AssetClass::Option, DataType::Bars)));
    assert!(!SUPPORTED_DATASETS.contains(&(AssetClass::Reference, DataType::Classification)));
}

#[test]
fn data_type_parses_and_renders_bars() {
    assert_eq!(DataType::Bars.value(), "bars");
    assert_eq!(DataType::from_value("bars"), Some(DataType::Bars));
    assert_eq!(DataType::Bars.to_string(), "bars");
}
