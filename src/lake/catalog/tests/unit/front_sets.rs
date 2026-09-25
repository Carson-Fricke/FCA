use super::*;
use crate::memory::MemoryContractRepository;
use crate::models::OptionType;
use lake_contracts::Decimal18;

fn contract(
    underlying: &str,
    symbol: &str,
    expiration_month: u32,
    expiration_day: u32,
    strike: &str,
    option_type: OptionType,
) -> ContractRecord {
    ContractRecord::new(
        symbol,
        underlying,
        Date::from_ymd(2024, expiration_month, expiration_day).expect("date"),
        Decimal18::parse(strike).expect("strike"),
        option_type,
        100,
        None,
        None,
    )
    .expect("valid contract")
}

#[test]
fn front_set_sorts_contracts_and_canonicalizes_underlying() {
    let front_set = DailyFrontSet::new(
        " spy ",
        Date::from_ymd(2024, 1, 1).expect("date"),
        vec![
            contract("SPY", "SPY240216P00500000", 2, 16, "500", OptionType::Put),
            contract("SPY", "SPY240119P00500000", 1, 19, "500", OptionType::Put),
            contract("SPY", "SPY240119C00500000", 1, 19, "500", OptionType::Call),
            contract("SPY", "SPY240119C00490000", 1, 19, "490", OptionType::Call),
        ],
    )
    .expect("valid front set");

    assert_eq!(front_set.underlying, "SPY");
    assert_eq!(
        front_set.key(),
        ("SPY".to_string(), Date::from_ymd(2024, 1, 1).expect("date"))
    );
    assert_eq!(
        front_set.symbols(),
        vec![
            "SPY240119C00490000".to_string(),
            "SPY240119C00500000".to_string(),
            "SPY240119P00500000".to_string(),
            "SPY240216P00500000".to_string(),
        ]
    );
    assert_eq!(
        front_set.expiration_dates(),
        vec![
            Date::from_ymd(2024, 1, 19).expect("date"),
            Date::from_ymd(2024, 2, 16).expect("date"),
        ]
    );
}

#[test]
fn front_set_rejects_foreign_and_duplicate_contracts() {
    let error = DailyFrontSet::new(
        "SPY",
        Date::from_ymd(2024, 1, 1).expect("date"),
        vec![contract(
            "QQQ",
            "QQQ240119C00500000",
            1,
            19,
            "500",
            OptionType::Call,
        )],
    )
    .expect_err("foreign underlying");
    assert_eq!(
        error,
        DataError::value("front-set contracts must share the selected underlying")
    );

    let error = DailyFrontSet::new(
        "SPY",
        Date::from_ymd(2024, 1, 1).expect("date"),
        vec![
            contract("SPY", "SPY240119C00500000", 1, 19, "500", OptionType::Call),
            contract("SPY", "SPY240119C00500000", 1, 19, "500", OptionType::Call),
        ],
    )
    .expect_err("duplicate symbols");
    assert_eq!(
        error,
        DataError::value("front-set contracts must not contain duplicate symbols")
    );

    let error = DailyFrontSet::new("  ", Date::from_ymd(2024, 1, 1).expect("date"), vec![])
        .expect_err("empty underlying");
    assert_eq!(error, DataError::value("underlying must not be empty"));
}

#[test]
fn front_set_round_trips_through_serde() {
    let front_set = DailyFrontSet::new(
        "SPY",
        Date::from_ymd(2024, 1, 1).expect("date"),
        vec![
            contract("SPY", "SPY240119C00500000", 1, 19, "500", OptionType::Call),
            contract("SPY", "SPY240216P00500000", 2, 16, "500", OptionType::Put),
        ],
    )
    .expect("valid front set");
    let json = serde_json::to_string(&front_set).expect("serialize");
    assert!(json.contains("\"selection_date\":\"2024-01-01\""));
    let parsed: DailyFrontSet = serde_json::from_str(&json).expect("round trip");
    assert_eq!(parsed, front_set);
}

#[test]
fn memory_front_set_repository_filters_and_sorts() {
    let first = DailyFrontSet::new(
        "SPY",
        Date::from_ymd(2024, 1, 2).expect("date"),
        vec![contract(
            "SPY",
            "SPY240119C00500000",
            1,
            19,
            "500",
            OptionType::Call,
        )],
    )
    .expect("valid front set");
    let second = DailyFrontSet::new("SPY", Date::from_ymd(2024, 1, 3).expect("date"), vec![])
        .expect("valid front set");
    let other = DailyFrontSet::new(
        "QQQ",
        Date::from_ymd(2024, 1, 2).expect("date"),
        vec![contract(
            "QQQ",
            "QQQ240119C00400000",
            1,
            19,
            "400",
            OptionType::Call,
        )],
    )
    .expect("valid front set");
    let repository =
        MemoryDailyFrontSetRepository::from_records([second.clone(), other.clone(), first.clone()]);

    assert_eq!(
        repository.get(" spy ", first.selection_date).expect("get"),
        Some(first.clone())
    );
    assert_eq!(
        repository
            .get("SPY", Date::from_ymd(2024, 1, 4).expect("date"))
            .expect("get"),
        None
    );
    assert_eq!(
        repository.list(None, None, None).expect("list"),
        vec![other.clone(), first.clone(), second.clone()]
    );
    assert_eq!(
        repository.list(Some("spy"), None, None).expect("list"),
        vec![first.clone(), second.clone()]
    );
    assert_eq!(
        repository
            .list(None, Some(Date::from_ymd(2024, 1, 3).expect("date")), None)
            .expect("list"),
        vec![second.clone()]
    );
    assert_eq!(
        repository
            .list(
                None,
                Some(Date::from_ymd(2024, 1, 3).expect("date")),
                Some(Date::from_ymd(2024, 1, 2).expect("date"))
            )
            .expect_err("reversed range"),
        DataError::value("start must be on or before end")
    );
}

#[test]
fn contracts_catalog_delegates_to_both_repositories() {
    let contracts: Arc<dyn ContractRepository> = Arc::new(MemoryContractRepository::new());
    let catalog = ContractsCatalog::with_contracts(contracts);
    let record = contract("SPY", "SPY240119C00500000", 1, 19, "500", OptionType::Call);
    catalog.upsert(&record).expect("upsert contract");
    assert_eq!(
        catalog.get("spy240119c00500000").expect("get"),
        Some(record)
    );

    let front_set = DailyFrontSet::new(
        "SPY",
        Date::from_ymd(2024, 1, 2).expect("date"),
        vec![contract(
            "SPY",
            "SPY240119C00500000",
            1,
            19,
            "500",
            OptionType::Call,
        )],
    )
    .expect("valid front set");
    catalog
        .upsert_front_set(&front_set)
        .expect("upsert front set");
    assert_eq!(
        catalog
            .get_front_set("SPY", front_set.selection_date)
            .expect("get front set"),
        Some(front_set.clone())
    );
    assert_eq!(
        catalog
            .list_front_sets(None, None, None)
            .expect("list front sets"),
        vec![front_set]
    );
    assert_eq!(
        catalog
            .list(Some("SPY"), None)
            .expect("list contracts")
            .len(),
        1
    );
}

#[test]
fn front_set_record_alias_is_the_daily_front_set() {
    fn take(_: FrontSetRecord) {}
    take(
        DailyFrontSet::new("SPY", Date::from_ymd(2024, 1, 1).expect("date"), vec![])
            .expect("valid front set"),
    );
}
