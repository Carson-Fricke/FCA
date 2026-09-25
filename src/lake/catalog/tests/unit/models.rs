use super::*;

fn sample_coverage() -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        " aapl ",
        Date::from_ymd(2024, 1, 19).expect("date"),
        CoverageStatus::Complete,
        Some("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet".to_string()),
        12,
        3456,
        Some("sha256:abc".to_string()),
        None,
    )
    .expect("valid coverage")
}

fn sample_listing() -> ListingRecord {
    ListingRecord::new(
        " meta ",
        " fb ",
        Date::from_ymd(2012, 5, 18).expect("date"),
        Some(Date::from_ymd(2022, 6, 8).expect("date")),
        ListingType::Ipo,
        Some(DelistingType::Rename),
        Some(Decimal18::parse("196.35").expect("price")),
    )
    .expect("valid listing")
}

#[test]
fn table_key_percent_encodes_like_python_quote() {
    assert_eq!(table_key("AAPL"), "AAPL");
    assert_eq!(table_key("job/2024-01-19"), "job%2F2024-01-19");
    assert_eq!(table_key("a b"), "a%20b");
    assert_eq!(table_key("caf\u{e9}"), "caf%C3%A9");
    assert_eq!(table_key("a_b.c-d~e"), "a_b.c-d~e");
    assert_eq!(table_key("A+B=C;D&E?F#G"), "A%2BB%3DC%3BD%26E%3FF%23G");
    assert_eq!(table_key(""), "");
}

#[test]
fn persisted_enums_round_trip_their_values() {
    for status in [
        CoverageStatus::Complete,
        CoverageStatus::Empty,
        CoverageStatus::Partial,
        CoverageStatus::Error,
    ] {
        assert_eq!(CoverageStatus::from_value(status.value()), Some(status));
        assert_eq!(status.to_string(), status.value());
    }
    for state in [
        JobState::Queued,
        JobState::Running,
        JobState::Complete,
        JobState::Succeeded,
        JobState::Failed,
        JobState::Cancelled,
    ] {
        assert_eq!(JobState::from_value(state.value()), Some(state));
        assert_eq!(state.to_string(), state.value());
    }
    for state in [
        RunState::Queued,
        RunState::Running,
        RunState::Complete,
        RunState::Failed,
    ] {
        assert_eq!(RunState::from_value(state.value()), Some(state));
        assert_eq!(state.to_string(), state.value());
    }
    for option in [OptionType::Call, OptionType::Put] {
        assert_eq!(OptionType::from_value(option.value()), Some(option));
    }
    for listing in [
        ListingType::Ipo,
        ListingType::Direct,
        ListingType::Spinoff,
        ListingType::Rename,
        ListingType::Transfer,
        ListingType::Unknown,
    ] {
        assert_eq!(ListingType::from_value(listing.value()), Some(listing));
    }
    for delisting in [
        DelistingType::Rename,
        DelistingType::Merger,
        DelistingType::Acquisition,
        DelistingType::Bankruptcy,
        DelistingType::Voluntary,
        DelistingType::Regulatory,
        DelistingType::Unknown,
    ] {
        assert_eq!(
            DelistingType::from_value(delisting.value()),
            Some(delisting)
        );
    }
    assert_eq!(CoverageStatus::from_value("bogus"), None);
    assert_eq!(
        CoverageRecord::new(
            AssetClass::Equity,
            DataType::Trade,
            "",
            Date::from_ymd(2024, 1, 19).expect("date"),
            CoverageStatus::Empty,
            None,
            0,
            0,
            None,
            None,
        )
        .expect_err("empty symbol"),
        DataError::value("symbol must not be empty")
    );
}

#[test]
fn coverage_record_canonicalizes_and_exposes_keys() {
    let record = sample_coverage();
    assert_eq!(record.symbol, "AAPL");
    assert_eq!(
        record.key(),
        (
            AssetClass::Equity,
            DataType::Trade,
            "AAPL".to_string(),
            Date::from_ymd(2024, 1, 19).expect("date"),
        )
    );
    assert_eq!(record.partition_key(), "coverage|equity|trade|AAPL");
    assert_eq!(record.row_key(), "2024-01-19");
    assert!(record.is_terminal());
    assert!(!record.replace_existing());

    let partial = CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        record.session_date,
        CoverageStatus::Partial,
        None,
        0,
        0,
        None,
        None,
    )
    .expect("valid");
    assert!(!partial.is_terminal());
    assert!(partial.replace_existing());
    for status in [CoverageStatus::Empty, CoverageStatus::Error] {
        let record = CoverageRecord::new(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            record.session_date,
            status,
            None,
            0,
            0,
            None,
            None,
        )
        .expect("valid");
        assert_eq!(record.is_terminal(), status != CoverageStatus::Error);
        assert!(!record.replace_existing());
    }
}

#[test]
fn coverage_record_serializes_with_the_ten_canonical_keys() {
    let record = sample_coverage();
    let json = serde_json::to_string(&record).expect("serialize");
    assert_eq!(
        json,
        concat!(
            "{\"asset_class\":\"equity\",\"byte_size\":3456,\"checksum\":\"sha256:abc\",",
            "\"data_type\":\"trade\",\"error_message\":null,",
            "\"object_path\":\"date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet\",",
            "\"row_count\":12,\"session_date\":\"2024-01-19\",\"status\":\"complete\",",
            "\"symbol\":\"AAPL\"}"
        )
    );
    let parsed: CoverageRecord = serde_json::from_str(&json).expect("round trip");
    assert_eq!(parsed, record);
}

#[test]
fn coverage_record_rejects_unexpected_and_missing_keys() {
    let missing = serde_json::json!({
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "AAPL",
        "session_date": "2024-01-19",
        "status": "complete"
    });
    let error = serde_json::from_value::<CoverageRecord>(missing).expect_err("missing keys");
    assert!(error
        .to_string()
        .contains("coverage row has unexpected fields"));

    let mut extra = serde_json::to_value(sample_coverage()).expect("value");
    extra["database"] = serde_json::json!("no");
    let error = serde_json::from_value::<CoverageRecord>(extra).expect_err("extra key");
    assert!(error
        .to_string()
        .contains("coverage row has unexpected fields"));
}

#[test]
fn job_record_trims_and_round_trips() {
    let created_at = DateTime::from_ymd_hms(2024, 1, 19, 12, 0, 0, 0).expect("datetime");
    let updated_at = DateTime::from_ymd_hms(2024, 1, 19, 12, 30, 0, 0).expect("datetime");
    let record = JobRecord::new(
        " job/2024-01-19 ",
        JobState::Failed,
        created_at,
        updated_at,
        Some("failed to fetch".to_string()),
    )
    .expect("valid job");
    assert_eq!(record.job_id, "job/2024-01-19");
    assert_eq!(record.row_key(), "job%2F2024-01-19");

    let json = serde_json::to_string(&record).expect("serialize");
    assert!(json.contains("\"created_at\":\"2024-01-19T12:00:00.000000+00:00\""));
    assert!(json.contains("\"state\":\"failed\""));
    let parsed: JobRecord = serde_json::from_str(&json).expect("round trip");
    assert_eq!(parsed, record);

    assert_eq!(
        JobRecord::new("   ", JobState::Queued, created_at, updated_at, None)
            .expect_err("empty job id"),
        DataError::value("job_id must not be empty")
    );
}

#[test]
fn run_record_validates_and_round_trips() {
    let metrics = BTreeMap::from([
        ("alpha".to_string(), "1".to_string()),
        ("beta".to_string(), "two".to_string()),
    ]);
    let record = RunRecord::new(
        " run-1 ",
        RunState::Complete,
        " short-put ",
        Some(Date::from_ymd(2024, 1, 1).expect("date")),
        Some(Date::from_ymd(2024, 1, 31).expect("date")),
        Some("run-digest".to_string()),
        Some("config-digest".to_string()),
        3,
        4096,
        metrics,
    )
    .expect("valid run");
    assert_eq!(record.run_id, "run-1");
    assert_eq!(record.strategy_id, "short-put");
    assert_eq!(record.row_key(), "run-1");

    let json = serde_json::to_string(&record).expect("serialize");
    assert!(json.contains("\"data_start\":\"2024-01-01\""));
    assert!(json.contains("\"metrics\":{\"alpha\":\"1\",\"beta\":\"two\"}"));
    let parsed: RunRecord = serde_json::from_str(&json).expect("round trip");
    assert_eq!(parsed, record);

    let base = || {
        RunRecord::new(
            "run-1",
            RunState::Queued,
            "strategy",
            None,
            None,
            None,
            None,
            0,
            0,
            BTreeMap::new(),
        )
    };
    assert_eq!(
        RunRecord::new(
            " ",
            RunState::Queued,
            "strategy",
            None,
            None,
            None,
            None,
            0,
            0,
            BTreeMap::new(),
        )
        .expect_err("empty run id"),
        DataError::value("run_id must not be empty")
    );
    assert_eq!(
        RunRecord::new(
            "run-1",
            RunState::Queued,
            "  ",
            None,
            None,
            None,
            None,
            0,
            0,
            BTreeMap::new(),
        )
        .expect_err("empty strategy"),
        DataError::value("strategy_id must not be empty")
    );
    assert_eq!(
        RunRecord::new(
            "run-1",
            RunState::Queued,
            "strategy",
            Some(Date::from_ymd(2024, 2, 1).expect("date")),
            Some(Date::from_ymd(2024, 1, 1).expect("date")),
            None,
            None,
            0,
            0,
            BTreeMap::new(),
        )
        .expect_err("reversed bounds"),
        DataError::value("data_start must be on or before data_end")
    );
    assert!(base().is_ok());
}

#[test]
fn contract_record_validates_and_round_trips() {
    let record = ContractRecord::new(
        " aapl240119c00150000 ",
        " aapl ",
        Date::from_ymd(2024, 1, 19).expect("date"),
        Decimal18::parse("150.125000000000000001").expect("strike"),
        OptionType::Call,
        100,
        Some(Date::from_ymd(2023, 1, 1).expect("date")),
        Some(Date::from_ymd(2024, 1, 19).expect("date")),
    )
    .expect("valid contract");
    assert_eq!(record.symbol, "AAPL240119C00150000");
    assert_eq!(record.underlying, "AAPL");
    assert_eq!(record.strike(), record.strike_price);

    let json = serde_json::to_string(&record).expect("serialize");
    assert!(json.contains("\"strike_price\":\"150.125000000000000001\""));
    assert!(json.contains("\"option_type\":\"call\""));
    assert!(json.contains("\"expiration_date\":\"2024-01-19\""));
    let parsed: ContractRecord = serde_json::from_str(&json).expect("round trip");
    assert_eq!(parsed, record);

    let build = |strike: &str, multiplier: u32, listed: Option<Date>, delisted: Option<Date>| {
        ContractRecord::new(
            "AAPL240119C00150000",
            "AAPL",
            Date::from_ymd(2024, 1, 19).expect("date"),
            Decimal18::parse(strike).expect("strike"),
            OptionType::Call,
            multiplier,
            listed,
            delisted,
        )
    };
    assert_eq!(
        build("0", 100, None, None).expect_err("zero strike"),
        DataError::value("strike_price must be positive")
    );
    assert_eq!(
        build("1", 0, None, None).expect_err("zero multiplier"),
        DataError::value("multiplier must be positive")
    );
    assert_eq!(
        build(
            "1",
            100,
            Some(Date::from_ymd(2024, 2, 1).expect("date")),
            Some(Date::from_ymd(2024, 1, 1).expect("date")),
        )
        .expect_err("reversed lifecycle"),
        DataError::value("listed_date must be on or before delisted_date")
    );
}

#[test]
fn listing_record_validates_canonicalizes_and_round_trips() {
    let record = sample_listing();
    assert_eq!(record.entity_id, "META");
    assert_eq!(record.symbol, "FB");
    assert_eq!(record.key(), ("META".to_string(), "FB".to_string()));
    assert_eq!(record.partition_key(), "listings|META");
    assert_eq!(record.row_key(), "FB");
    assert_eq!(record.listing_type, ListingType::Ipo);
    assert_eq!(record.delisting_type, Some(DelistingType::Rename));
    assert_eq!(
        record.final_price,
        Some(Decimal18::parse("196.35").expect("price"))
    );

    let json = serde_json::to_string(&record).expect("serialize");
    assert!(json.contains("\"final_price\":\"196.35\""));
    assert!(json.contains("\"delisting_type\":\"rename\""));
    let parsed: ListingRecord = serde_json::from_str(&json).expect("round trip");
    assert_eq!(parsed, record);

    let build = |entity: &str,
                 delisted: Option<Date>,
                 delisting: Option<DelistingType>,
                 price: Option<Decimal18>| {
        ListingRecord::new(
            entity,
            "FB",
            Date::from_ymd(2012, 5, 18).expect("date"),
            delisted,
            ListingType::Ipo,
            delisting,
            price,
        )
    };
    assert_eq!(
        build(" ", None, None, None).expect_err("empty entity"),
        DataError::value("entity_id must not be empty")
    );
    assert_eq!(
        build(
            "META",
            Some(Date::from_ymd(2012, 5, 17).expect("date")),
            Some(DelistingType::Rename),
            None,
        )
        .expect_err("reversed lifecycle"),
        DataError::value("listed_date must be on or before delisted_date")
    );
    assert_eq!(
        build("META", None, Some(DelistingType::Merger), None).expect_err("orphan delisting"),
        DataError::value("delisting_type requires a delisted_date")
    );
    assert_eq!(
        build(
            "META",
            Some(Date::from_ymd(2022, 6, 8).expect("date")),
            None,
            None,
        )
        .expect_err("orphan delisted date"),
        DataError::value("delisted_date requires a delisting_type")
    );
    assert_eq!(
        build(
            "META",
            None,
            None,
            Some(Decimal18::parse("10").expect("price")),
        )
        .expect_err("price without delisting"),
        DataError::value("final_price requires a delisted_date")
    );
    assert_eq!(
        build(
            "META",
            Some(Date::from_ymd(2022, 6, 8).expect("date")),
            Some(DelistingType::Rename),
            Some(Decimal18::parse("0").expect("price")),
        )
        .expect_err("non-positive price"),
        DataError::value("final_price must be positive")
    );
}

#[test]
fn listing_activity_is_point_in_time() {
    let record = sample_listing();
    let known_day = Date::from_ymd(2024, 1, 2).expect("date");

    assert!(record.is_known(Date::from_ymd(2012, 5, 18).expect("date")));
    assert!(!record.is_known(Date::from_ymd(2012, 5, 17).expect("date")));
    assert!(record.active_on(Date::from_ymd(2020, 1, 2).expect("date"), known_day));
    assert!(record.active_on(Date::from_ymd(2022, 6, 8).expect("date"), known_day));
    assert!(!record.active_on(Date::from_ymd(2022, 6, 9).expect("date"), known_day));
    assert!(!record.active_on(Date::from_ymd(2010, 1, 2).expect("date"), known_day));
    assert!(!record.active_on(
        Date::from_ymd(2020, 1, 2).expect("date"),
        Date::from_ymd(2012, 5, 17).expect("date")
    ));
}

#[test]
fn unknown_delisting_keeps_the_listing_active() {
    let pending = ListingRecord::new(
        "EARLY",
        "EARLY",
        Date::from_ymd(2020, 2, 20).expect("date"),
        Some(Date::from_ymd(2026, 12, 31).expect("date")),
        ListingType::Ipo,
        Some(DelistingType::Voluntary),
        Some(Decimal18::parse("10.00").expect("price")),
    )
    .expect("valid listing");
    let early = Date::from_ymd(2024, 6, 28).expect("date");
    let late = Date::from_ymd(2027, 1, 4).expect("date");

    assert!(pending.active_on(early, early));
    assert!(pending.active_on(late, early));
    assert!(!pending.active_on(late, late));
    assert!(pending.active_on(early, late));
}
