//! Repository parity tests: the memory adapters, the indexed JSON store, and
//! the composed contracts catalog behave like the Python memory repositories
//! for their sorted orders, filters, and point-in-time semantics.

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use lake_catalog::{
    table_key, CatalogRepository, ContractRecord, ContractRepository, ContractsCatalog,
    CoverageQuery, CoverageRecord, CoverageStatus, DailyFrontSet, DailyFrontSetRepository,
    DelistingType, JobRecord, JobRepository, JobState, JsonCatalogStore, ListingRecord,
    ListingRepository, ListingType, MemoryCatalogRepository, MemoryContractRepository,
    MemoryDailyFrontSetRepository, MemoryJobRepository, MemoryListingRepository,
    MemoryRunRepository, OptionType, RunRecord, RunRepository, RunState,
};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime, Decimal18};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("date")
}

fn instant(hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, hour, minute, 0, 0).expect("datetime")
}

fn coverage(status: CoverageStatus) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        " aapl ",
        day(2024, 1, 19),
        status,
        Some("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet".to_string()),
        12,
        3456,
        Some("sha256:abc".to_string()),
        None,
    )
    .expect("valid coverage")
}

fn msft_coverage() -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "MSFT",
        day(2024, 1, 18),
        CoverageStatus::Empty,
        None,
        0,
        0,
        None,
        None,
    )
    .expect("valid coverage")
}

fn job(state: JobState) -> JobRecord {
    JobRecord::new(
        " job/2024-01-19 ",
        state,
        instant(12, 0),
        instant(12, 30),
        None,
    )
    .expect("valid job")
}

fn run() -> RunRecord {
    RunRecord::new(
        "run-1",
        RunState::Complete,
        "short-put",
        Some(day(2024, 1, 1)),
        Some(day(2024, 1, 31)),
        Some("run-digest".to_string()),
        Some("config-digest".to_string()),
        3,
        4096,
        BTreeMap::from([("alpha".to_string(), "1".to_string())]),
    )
    .expect("valid run")
}

fn contract(
    symbol: &str,
    underlying: &str,
    listed: Option<Date>,
    delisted: Option<Date>,
) -> ContractRecord {
    ContractRecord::new(
        symbol,
        underlying,
        day(2024, 1, 19),
        Decimal18::parse("150.125000000000000001").expect("strike"),
        OptionType::Call,
        100,
        listed,
        delisted,
    )
    .expect("valid contract")
}

fn episode() -> ListingRecord {
    ListingRecord::new(
        " meta ",
        " fb ",
        day(2012, 5, 18),
        Some(day(2022, 6, 8)),
        ListingType::Ipo,
        Some(DelistingType::Rename),
        Some(Decimal18::parse("196.35").expect("price")),
    )
    .expect("valid listing")
}

fn successor() -> ListingRecord {
    ListingRecord::new(
        "META",
        "META",
        day(2022, 6, 9),
        None,
        ListingType::Rename,
        None,
        None,
    )
    .expect("valid listing")
}

fn front_set(underlying: &str, selection_date: Date) -> DailyFrontSet {
    DailyFrontSet::new(
        underlying,
        selection_date,
        vec![ContractRecord::new(
            format!("{underlying}240119C00150000"),
            underlying,
            day(2024, 1, 19),
            Decimal18::parse("150").expect("strike"),
            OptionType::Call,
            100,
            None,
            None,
        )
        .expect("valid contract")],
    )
    .expect("valid front set")
}

fn temp_directory(label: &str) -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let path = std::env::temp_dir().join(format!(
        "lake-catalog-integration-{label}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).expect("temp dir");
    path
}

fn assert_send_sync<T: Send + Sync>(_: &T) {}

#[test]
fn catalog_memory_matches_the_python_fixture() {
    let repository =
        MemoryCatalogRepository::from_records([msft_coverage(), coverage(CoverageStatus::Partial)]);
    assert_send_sync(&repository);
    let repository: &dyn CatalogRepository = &repository;

    assert_eq!(
        repository
            .get(
                AssetClass::Equity,
                DataType::Trade,
                "aapl",
                day(2024, 1, 19)
            )
            .expect("get"),
        Some(coverage(CoverageStatus::Partial))
    );
    assert_eq!(
        repository
            .get(
                AssetClass::Equity,
                DataType::Trade,
                "AAPL",
                day(2024, 1, 20)
            )
            .expect("get"),
        None
    );
    let listed = repository.list(&CoverageQuery::default()).expect("list");
    let symbols: Vec<&str> = listed.iter().map(|record| record.symbol.as_str()).collect();
    assert_eq!(symbols, ["AAPL", "MSFT"]);

    let aapl_only = CoverageQuery {
        symbol: Some("aapl".to_string()),
        ..CoverageQuery::default()
    };
    assert_eq!(
        repository.list(&aapl_only).expect("list"),
        vec![coverage(CoverageStatus::Partial)]
    );

    let ranged = CoverageQuery {
        start: Some(day(2024, 1, 19)),
        end: Some(day(2024, 1, 19)),
        ..CoverageQuery::default()
    };
    assert_eq!(repository.list(&ranged).expect("list").len(), 1);

    let reversed = CoverageQuery {
        start: Some(day(2024, 1, 2)),
        end: Some(day(2024, 1, 1)),
        ..CoverageQuery::default()
    };
    assert_eq!(
        repository.list(&reversed).expect_err("reversed"),
        DataError::value("start must be on or before end")
    );
}

#[test]
fn json_store_behaves_identically_and_persists() {
    let directory = temp_directory("parity");
    let path = directory.join("catalog.json");
    let memory =
        MemoryCatalogRepository::from_records([msft_coverage(), coverage(CoverageStatus::Partial)]);
    let store = JsonCatalogStore::open(&path).expect("open");
    assert_send_sync(&store);

    let fixtures = [msft_coverage(), coverage(CoverageStatus::Partial)];
    store.upsert_many(&fixtures).expect("upsert");
    assert_eq!(
        store.list(&CoverageQuery::default()).expect("list"),
        memory.list(&CoverageQuery::default()).expect("list")
    );
    assert_eq!(
        store
            .get(
                AssetClass::Equity,
                DataType::Trade,
                " aapl ",
                day(2024, 1, 19)
            )
            .expect("get"),
        memory
            .get(
                AssetClass::Equity,
                DataType::Trade,
                " aapl ",
                day(2024, 1, 19)
            )
            .expect("get")
    );

    let reopened = JsonCatalogStore::open(&path).expect("reopen");
    assert_eq!(
        reopened.list(&CoverageQuery::default()).expect("list"),
        memory.list(&CoverageQuery::default()).expect("list")
    );
    let _ = fs::remove_dir_all(&directory);
}

#[test]
fn job_and_run_repositories_filter_and_sort() {
    let jobs = MemoryJobRepository::from_records([job(JobState::Queued)]);
    let jobs: &dyn JobRepository = &jobs;
    assert_eq!(
        jobs.get("job/2024-01-19").expect("get"),
        Some(job(JobState::Queued))
    );
    assert_eq!(
        jobs.get(" job/2024-01-19 ").expect("get"),
        Some(job(JobState::Queued))
    );
    assert_eq!(
        jobs.list(Some(JobState::Queued)).expect("list"),
        vec![job(JobState::Queued)]
    );
    assert_eq!(jobs.list(Some(JobState::Failed)).expect("list"), vec![]);
    assert_eq!(jobs.list(None).expect("list").len(), 1);

    let runs = MemoryRunRepository::from_records([run()]);
    let runs: &dyn RunRepository = &runs;
    assert_eq!(runs.get(" run-1 ").expect("get"), Some(run()));
    assert_eq!(
        runs.list(Some(RunState::Complete), Some(" short-put "))
            .expect("list"),
        vec![run()]
    );
    assert_eq!(
        runs.list(Some(RunState::Failed), None).expect("list"),
        vec![]
    );
    assert_eq!(runs.list(None, Some("other")).expect("list"), vec![]);
    assert!(runs.delete("run-1").expect("delete"));
    assert!(!runs.delete("run-1").expect("delete"));
    assert_eq!(runs.get("run-1").expect("get"), None);
}

#[test]
fn contract_repository_active_rule_is_inclusive() {
    let dated = contract(
        "AAPL240119C00150000",
        "AAPL",
        Some(day(2023, 1, 1)),
        Some(day(2024, 1, 19)),
    );
    let open = contract("SPY240119C00500000", "SPY", None, None);
    let repository = MemoryContractRepository::from_records([open.clone(), dated.clone()]);
    let repository: &dyn ContractRepository = &repository;

    assert_eq!(
        repository.get("aapl240119c00150000").expect("get"),
        Some(dated.clone())
    );
    assert_eq!(
        repository.list(Some("aapl"), None).expect("list"),
        vec![dated.clone()]
    );
    assert_eq!(
        repository.list(None, Some(day(2023, 1, 1))).expect("list"),
        vec![dated.clone(), open.clone()]
    );
    assert_eq!(
        repository.list(None, Some(day(2024, 1, 19))).expect("list"),
        vec![dated.clone(), open.clone()]
    );
    assert_eq!(
        repository.list(None, Some(day(2024, 1, 20))).expect("list"),
        vec![open.clone()]
    );
    assert_eq!(
        repository
            .list(None, Some(day(2022, 12, 31)))
            .expect("list"),
        vec![open]
    );
    assert_eq!(
        repository.list(Some("missing"), None).expect("list"),
        vec![]
    );
}

#[test]
fn listing_repository_matches_python_point_in_time_semantics() {
    let repository = MemoryListingRepository::from_records([episode(), successor()]);
    let repository: &dyn ListingRepository = &repository;

    assert_eq!(
        repository.get(" meta ", "fb").expect("get"),
        Some(episode())
    );
    assert_eq!(repository.get("META", "UNKNOWN").expect("get"), None);
    assert_eq!(
        repository
            .list(None, None, None)
            .expect("list")
            .iter()
            .map(|record| record.symbol.as_str())
            .collect::<Vec<_>>(),
        ["FB", "META"]
    );
    assert_eq!(
        repository.list(Some("meta"), None, None).expect("list"),
        vec![episode(), successor()]
    );
    assert_eq!(
        repository.list(None, Some("fb"), None).expect("list"),
        vec![episode()]
    );
    assert_eq!(
        repository
            .list(None, None, Some(day(2020, 1, 2)))
            .expect("list"),
        vec![episode()]
    );
    assert_eq!(
        repository
            .list(None, None, Some(day(2012, 5, 17)))
            .expect("list"),
        vec![]
    );
    assert_eq!(
        repository
            .list(None, None, Some(day(2022, 6, 9)))
            .expect("list"),
        vec![episode(), successor()]
    );
    assert_eq!(
        repository.get("  ", "FB").expect_err("empty entity"),
        DataError::value("entity_id must not be empty")
    );
    assert_eq!(table_key("job/2024-01-19"), "job%2F2024-01-19");
}

#[test]
fn front_sets_and_contracts_catalog_compose() {
    let front_sets = MemoryDailyFrontSetRepository::from_records([
        front_set("SPY", day(2024, 1, 2)),
        front_set("QQQ", day(2024, 1, 2)),
        front_set("SPY", day(2024, 1, 3)),
    ]);
    let front_sets: &dyn DailyFrontSetRepository = &front_sets;

    assert_eq!(
        front_sets.get("SPY", day(2024, 1, 2)).expect("get"),
        Some(front_set("SPY", day(2024, 1, 2)))
    );
    let listed = front_sets.list(None, None, None).expect("list");
    let symbols: Vec<&str> = listed
        .iter()
        .map(|front_set| front_set.underlying.as_str())
        .collect();
    assert_eq!(symbols, ["QQQ", "SPY", "SPY"]);
    assert_eq!(
        front_sets
            .list(Some("spy"), Some(day(2024, 1, 3)), None)
            .expect("list"),
        vec![front_set("SPY", day(2024, 1, 3))]
    );
    assert_eq!(
        front_sets
            .list(None, Some(day(2024, 1, 3)), Some(day(2024, 1, 2)))
            .expect_err("reversed"),
        DataError::value("start must be on or before end")
    );

    let contracts: Arc<dyn ContractRepository> = Arc::new(MemoryContractRepository::from_records(
        [contract("SPY240119C00150000", "SPY", None, None)],
    ));
    let catalog = ContractsCatalog::with_contracts(contracts);
    let set = front_set("SPY", day(2024, 1, 2));
    catalog.upsert_front_set(&set).expect("upsert front set");
    assert_eq!(
        catalog.get_front_set("SPY", day(2024, 1, 2)).expect("get"),
        Some(set.clone())
    );
    assert_eq!(
        catalog
            .list_front_sets(Some("SPY"), None, None)
            .expect("list"),
        vec![set]
    );
    assert_eq!(catalog.list(Some("SPY"), None).expect("list").len(), 1);
}
