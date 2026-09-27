use std::path::Path;

use lake_contracts::{Date, DateTime};

use super::*;

fn fixtures_dir() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

const HEADER: &str = "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares";
const AAPL_ROW: &str = "Y|AAPL|Apple Inc. Common Stock|Q|Q|N|100|N|Q|AAPL|AAPL|N";
const CREATED: &str = "0419202417:20##";

#[test]
fn golden_snapshot_parses_securities_and_creation_instant() {
    let payload = std::fs::read(fixtures_dir().join("nasdaqtraded.txt")).expect("fixture");
    let snapshot = parse_nasdaq_traded(&payload).expect("snapshot");

    assert_eq!(
        snapshot.snapshot_date,
        Date::from_ymd(2024, 4, 19).expect("date")
    );
    assert_eq!(
        snapshot.file_creation_time.utc(),
        DateTime::from_ymd_hms(2024, 4, 19, 21, 20, 0, 0).expect("utc")
    );
    assert_eq!(
        snapshot.file_creation_time.isoformat(),
        "2024-04-19T17:20:00-04:00"
    );
    assert_eq!(
        snapshot
            .securities
            .iter()
            .map(|security| security.symbol.as_str())
            .collect::<Vec<_>>(),
        ["AAPL", "GOOG", "MSFT", "ZVZZT"]
    );
}

#[test]
fn golden_snapshot_security_fields_are_exact() {
    let payload = std::fs::read(fixtures_dir().join("nasdaqtraded.txt")).expect("fixture");
    let snapshot = parse_nasdaq_traded(&payload).expect("snapshot");
    let by_symbol: std::collections::HashMap<&str, &NasdaqSecurity> = snapshot
        .securities
        .iter()
        .map(|security| (security.symbol.as_str(), security))
        .collect();

    let aapl = by_symbol["AAPL"];
    assert_eq!(aapl.security_name, "Apple Inc. Common Stock");
    assert_eq!(aapl.listing_exchange, "Q");
    assert_eq!(aapl.market_category, "Q");
    assert!(!aapl.etf);
    assert_eq!(aapl.round_lot_size, Decimal18::parse("100").expect("lot"));
    assert!(!aapl.test_issue);
    assert_eq!(aapl.financial_status.as_deref(), Some("Q"));

    let goog = by_symbol["GOOG"];
    assert_eq!(goog.listing_exchange, "N");
    assert_eq!(goog.financial_status, None);
    assert!(!goog.etf);
    assert!(!goog.test_issue);

    let zvzzt = by_symbol["ZVZZT"];
    assert!(zvzzt.test_issue);
    assert_eq!(zvzzt.financial_status, None);
}

#[test]
fn rows_are_sorted_by_symbol_regardless_of_feed_order() {
    let snapshot = parse_nasdaq_traded(
        format!(
            "{HEADER}\n{}\n{}\nFile Creation Time|{CREATED}\n",
            "Y|ZZZZ|Zed Security|Q|Q|N|100|N||ZZZZ|ZZZZ|N",
            "Y|AAAA|Alpha Security|Q|Q|N|100|N||AAAA|AAAA|N",
        )
        .as_bytes(),
    )
    .expect("snapshot");

    assert_eq!(
        snapshot
            .securities
            .iter()
            .map(|security| security.symbol.as_str())
            .collect::<Vec<_>>(),
        ["AAAA", "ZZZZ"]
    );
}

#[test]
fn file_creation_time_accepts_compact_and_single_hash_variants() {
    for footer in ["0419202417:20##", "0419202417:20#", "041920241720##"] {
        let payload = format!("{HEADER}\n{AAPL_ROW}\nFile Creation Time|{footer}\n");
        let snapshot = parse_nasdaq_traded(payload.as_bytes()).expect("snapshot");
        assert_eq!(
            snapshot.file_creation_time.utc(),
            DateTime::from_ymd_hms(2024, 4, 19, 21, 20, 0, 0).expect("utc")
        );
    }
}

#[test]
fn file_creation_time_requires_the_hash_terminator() {
    for footer in ["0419202417:20", "0419202417:20###"] {
        let payload = format!("{HEADER}\n{AAPL_ROW}\nFile Creation Time|{footer}\n");
        let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
        assert!(error.to_string().contains("invalid file creation time"));
    }
}

#[test]
fn utf8_bom_is_tolerated() {
    let payload = format!("\u{feff}{HEADER}\n{AAPL_ROW}\nFile Creation Time|{CREATED}\n");
    let snapshot = parse_nasdaq_traded(payload.as_bytes()).expect("snapshot");
    assert_eq!(snapshot.securities.len(), 1);
    assert_eq!(snapshot.securities[0].symbol, "AAPL");
}

#[test]
fn blank_lines_are_ignored() {
    let payload = format!("{HEADER}\n\n{AAPL_ROW}\n\nFile Creation Time|{CREATED}\n\n");
    let snapshot = parse_nasdaq_traded(payload.as_bytes()).expect("snapshot");
    assert_eq!(snapshot.securities.len(), 1);
}

#[test]
fn unexpected_header_is_rejected() {
    let payload = format!(
        "Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n{AAPL_ROW}\nFile Creation Time|{CREATED}\n"
    );
    let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
    assert!(error.to_string().contains("unexpected nasdaqtraded header"));
}

#[test]
fn missing_file_creation_line_is_rejected() {
    let payload = format!("{HEADER}\n{AAPL_ROW}\n{AAPL_ROW}\n");
    let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
    assert!(error.to_string().contains("missing file creation line"));
}

#[test]
fn malformed_file_creation_time_is_rejected() {
    for footer in [
        "2024-04-19 17:20##",
        "1320240317:20##",
        "0419202417##",
        "garbage",
        "",
    ] {
        let payload = format!("{HEADER}\n{AAPL_ROW}\nFile Creation Time|{footer}\n");
        let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
        assert!(error.to_string().contains("invalid file creation time"));
    }
}

#[test]
fn row_with_wrong_field_count_is_rejected() {
    let payload = format!(
        "{HEADER}\nY|AAPL|Apple Inc. Common Stock|Q|Q|N|100|N|Q|AAPL|AAPL\nFile Creation Time|{CREATED}\n"
    );
    let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
    assert!(error.to_string().contains("line 2 has 11 of 12 fields"));
}

#[test]
fn row_with_invalid_flag_is_rejected() {
    let payload = format!(
        "{HEADER}\nY|AAPL|Apple Inc. Common Stock|Q|Q|yes|100|N|Q|AAPL|AAPL|N\nFile Creation Time|{CREATED}\n"
    );
    let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
    assert!(error.to_string().contains("flag must be Y or N"));
}

#[test]
fn row_with_invalid_round_lot_size_is_rejected() {
    for round_lot in ["0", "-100", "many"] {
        let payload = format!(
            "{HEADER}\nY|AAPL|Apple Inc. Common Stock|Q|Q|N|{round_lot}|N|Q|AAPL|AAPL|N\nFile Creation Time|{CREATED}\n"
        );
        let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
        assert!(error.to_string().contains("round lot size"));
    }
}

#[test]
fn row_with_empty_symbol_or_name_is_rejected() {
    for line in [
        "Y||Apple Inc. Common Stock|Q|Q|N|100|N|Q|AAPL|AAPL|N",
        "Y|AAPL||Q|Q|N|100|N|Q|AAPL|AAPL|N",
    ] {
        let payload = format!("{HEADER}\n{line}\nFile Creation Time|{CREATED}\n");
        let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
        assert!(error.to_string().contains("empty"));
    }
}

#[test]
fn payload_with_fewer_than_three_lines_is_rejected() {
    for payload in ["".to_string(), HEADER.to_string()] {
        let error = parse_nasdaq_traded(payload.as_bytes()).expect_err("rejected");
        assert!(error.to_string().contains("header, securities"));
    }
}

#[test]
fn dst_dates_resolve_to_the_right_utc_instant() {
    // EST: 2024-01-15 17:20 Eastern is 22:20 UTC.
    let payload = format!("{HEADER}\n{AAPL_ROW}\nFile Creation Time|0115202417:20##\n");
    let snapshot = parse_nasdaq_traded(payload.as_bytes()).expect("snapshot");
    assert_eq!(
        snapshot.file_creation_time.utc(),
        DateTime::from_ymd_hms(2024, 1, 15, 22, 20, 0, 0).expect("utc")
    );
    assert_eq!(
        snapshot.snapshot_date,
        Date::from_ymd(2024, 1, 15).expect("date")
    );
}
