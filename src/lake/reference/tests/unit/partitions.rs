use lake_contracts::{AssetClass, DataType, Date};

use super::*;

fn date(y: i32, m: u32, d: u32) -> Date {
    Date::from_ymd(y, m, d).expect("valid date")
}

#[test]
fn reference_partition_paths_follow_the_date_first_hive_contract() {
    assert_eq!(
        reference_partition_path(
            date(2024, 4, 19),
            DataType::Classification,
            CLASSIFICATION_OBJECT_NAME,
        )
        .expect("path"),
        "date=2024-04-19/asset_class=reference/data_type=classification/SNAPSHOT.parquet"
    );
    assert_eq!(
        reference_partition_path(
            date(2024, 2, 2),
            DataType::Fundamentals,
            &cik_object_name(320193).expect("name"),
        )
        .expect("path"),
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/CIK0000320193.parquet"
    );
}

#[test]
fn object_names_are_uppercased_and_reuse_symbol_filename_rules() {
    let path = reference_partition_path(date(2024, 4, 19), DataType::Classification, "snapshot")
        .expect("path");
    assert!(path.ends_with("/SNAPSHOT.parquet"));
}

#[test]
fn non_reference_data_types_are_rejected() {
    for data_type in [DataType::Trade, DataType::Quote] {
        let error = reference_partition_path(date(2024, 4, 19), data_type, "SNAPSHOT")
            .expect_err("rejected");
        assert!(error.to_string().contains("not a reference dataset"));
    }
}

#[test]
fn empty_object_names_are_rejected() {
    let error = reference_partition_path(date(2024, 4, 19), DataType::Classification, "  ")
        .expect_err("rejected");
    assert!(error.to_string().contains("must not be empty"));
}

#[test]
fn market_symbols_reuse_the_market_data_filename_rules() {
    let error = reference_partition_path(date(2024, 4, 19), DataType::Classification, "A|B")
        .expect_err("rejected");
    assert!(error.to_string().contains("invalid market symbol"));
}

#[test]
fn cik_symbols_are_zero_padded_ten_digit_canonical_values() {
    assert_eq!(cik_symbol(320193).expect("symbol"), "0000320193");
    assert_eq!(cik_object_name(320193).expect("name"), "CIK0000320193");
    assert_eq!(parse_cik_symbol("0000320193").expect("cik"), 320193);
    assert_eq!(
        parse_cik_symbol(&cik_symbol(1).expect("symbol")).expect("cik"),
        1
    );
}

#[test]
fn out_of_domain_ciks_are_rejected() {
    for cik in [0u64, 10u64.pow(10), 10u64.pow(12)] {
        assert!(cik_symbol(cik).is_err());
        assert!(cik_object_name(cik).is_err());
    }
}

#[test]
fn invalid_cik_symbols_are_rejected() {
    for value in ["", "320193", "00032001930", "00A0320193"] {
        let error = parse_cik_symbol(value).expect_err("rejected");
        assert!(error.to_string().contains("invalid reference CIK symbol"));
    }
}

#[test]
fn asset_class_values_stay_stable() {
    assert_eq!(AssetClass::Reference.value(), "reference");
    assert_eq!(DataType::Classification.value(), "classification");
    assert_eq!(DataType::Fundamentals.value(), "fundamentals");
}
