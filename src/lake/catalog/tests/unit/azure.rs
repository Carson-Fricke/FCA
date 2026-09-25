//! Hermetic tests for the Azure Table catalog adapters, ported from
//! `tests/lake/catalog/test_catalog.py`, `test_run_repository.py`, and
//! `test_listings.py`.

use std::collections::BTreeMap;
use std::sync::Mutex;

use super::*;
use crate::models::{table_key, CoverageStatus, JobState, RunState};
use lake_contracts::{AssetClass, DataType, Date, DateTime, Decimal18};

/// Duck-typed `TableClient` fake mirroring the Python tests.
#[derive(Default)]
struct FakeTable {
    entities: Mutex<BTreeMap<(String, String), TableEntity>>,
    queries: Mutex<Vec<(String, BTreeMap<String, String>)>>,
    deletes: Mutex<Vec<(String, String)>>,
}

impl FakeTable {
    fn entities(&self) -> BTreeMap<(String, String), TableEntity> {
        self.entities
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    fn queries(&self) -> Vec<(String, BTreeMap<String, String>)> {
        self.queries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }
}

impl TableEntityStore for FakeTable {
    fn get_entity(&self, partition_key: &str, row_key: &str) -> Result<TableEntity, DataError> {
        self.entities()
            .get(&(partition_key.to_string(), row_key.to_string()))
            .cloned()
            .ok_or_else(|| DataError::NotFound("missing".to_string()))
    }

    fn upsert_entity(&self, entity: TableEntity) -> Result<(), DataError> {
        let partition = entity["PartitionKey"].as_str().unwrap_or_default();
        let row = entity["RowKey"].as_str().unwrap_or_default();
        self.entities
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert((partition.to_string(), row.to_string()), entity);
        Ok(())
    }

    fn query_entities(&self, query: &TableQuery) -> Result<Vec<TableEntity>, DataError> {
        self.queries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push((query.filter.clone(), query.parameters.clone()));
        let entities = self.entities();
        let partition = query.parameters.get("partition");
        if let Some(partition) = partition {
            return Ok(entities
                .into_values()
                .filter(|entity| entity["PartitionKey"].as_str() == Some(partition.as_str()))
                .collect());
        }
        let start = query.parameters.get("start");
        let end = query.parameters.get("end");
        match (start, end) {
            (Some(start), Some(end)) => Ok(entities
                .into_values()
                .filter(|entity| {
                    let key = entity["PartitionKey"].as_str().unwrap_or_default();
                    key.starts_with(start.as_str()) && key < end.as_str()
                })
                .collect()),
            _ => Ok(entities.into_values().collect()),
        }
    }

    fn delete_entity(&self, partition_key: &str, row_key: &str) -> Result<(), DataError> {
        self.deletes
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push((partition_key.to_string(), row_key.to_string()));
        self.entities
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(&(partition_key.to_string(), row_key.to_string()));
        Ok(())
    }
}

const STATUSES: [CoverageStatus; 4] = [
    CoverageStatus::Complete,
    CoverageStatus::Empty,
    CoverageStatus::Partial,
    CoverageStatus::Error,
];

const STATES: [JobState; 6] = [
    JobState::Queued,
    JobState::Running,
    JobState::Complete,
    JobState::Succeeded,
    JobState::Failed,
    JobState::Cancelled,
];

const RUN_STATES: [RunState; 4] = [
    RunState::Queued,
    RunState::Running,
    RunState::Complete,
    RunState::Failed,
];

fn session_date() -> Date {
    Date::from_ymd(2024, 1, 19).expect("session date")
}

fn coverage(status: CoverageStatus) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        " aapl ",
        session_date(),
        status,
        Some("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet".to_string()),
        12,
        3456,
        Some("sha256:abc".to_string()),
        (status == CoverageStatus::Error).then(|| "source unavailable".to_string()),
    )
    .expect("coverage record")
}

fn job_created_at() -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, 12, 0, 0, 0).expect("created at")
}

fn job_updated_at() -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, 12, 30, 0, 0).expect("updated at")
}

fn job(state: JobState) -> JobRecord {
    JobRecord::new(
        " job/2024-01-19 ",
        state,
        job_created_at(),
        job_updated_at(),
        (state == JobState::Failed).then(|| "failed to fetch".to_string()),
    )
    .expect("job record")
}

fn contract() -> ContractRecord {
    ContractRecord::new(
        " aapl240119c00150000 ",
        " aapl ",
        Date::from_ymd(2024, 1, 19).expect("expiration"),
        Decimal18::parse("150.125000000000000001").expect("strike"),
        OptionType::Call,
        100,
        Some(Date::from_ymd(2023, 1, 1).expect("listed")),
        Some(session_date()),
    )
    .expect("contract record")
}

fn run(status: RunState) -> RunRecord {
    let mut metrics = BTreeMap::new();
    metrics.insert("max_drawdown".to_string(), "-0.0825".to_string());
    metrics.insert("sharpe".to_string(), "1.2345".to_string());
    RunRecord::new(
        "run-2024-01-02",
        status,
        "momentum",
        Date::from_ymd(2024, 1, 2),
        Date::from_ymd(2024, 1, 3),
        Some("digest-1".to_string()),
        Some("config-1".to_string()),
        2,
        24,
        metrics,
    )
    .expect("run record")
}

fn episode() -> ListingRecord {
    ListingRecord::new(
        " meta ",
        " fb ",
        Date::from_ymd(2012, 5, 18).expect("listed"),
        Date::from_ymd(2022, 6, 8),
        ListingType::Ipo,
        Some(DelistingType::Rename),
        Some(Decimal18::parse("196.35").expect("final price")),
    )
    .expect("listing record")
}

fn successor() -> ListingRecord {
    ListingRecord::new(
        "META",
        "META",
        Date::from_ymd(2022, 6, 9).expect("listed"),
        None,
        ListingType::Rename,
        None,
        None,
    )
    .expect("listing record")
}

#[test]
fn coverage_status_round_trips_through_entity() {
    for status in STATUSES {
        let record = coverage(status);
        assert_eq!(
            coverage_from_entity(&coverage_to_entity(&record)),
            Ok(record)
        );
    }
}

#[test]
fn job_state_and_utc_timestamps_round_trip_through_entity() {
    for state in STATES {
        let record = job(state);
        let entity = job_to_entity(&record);

        assert_eq!(entity["PartitionKey"].as_str(), Some("jobs"));
        assert_eq!(entity["RowKey"].as_str(), Some("job%2F2024-01-19"));
        assert_eq!(job_from_entity(&entity), Ok(record));
    }
}

#[test]
fn contract_decimal_and_lifecycle_dates_round_trip_through_entity() {
    let record = contract();
    let entity = contract_to_entity(&record);

    assert_eq!(
        entity["StrikePrice"].as_str(),
        Some("150.125000000000000001")
    );
    assert_eq!(contract_from_entity(&entity), Ok(record));
}

#[test]
fn listing_round_trips_through_entity_with_partition_keys() {
    let record = episode();
    assert_eq!(listing_partition_key(&record), "listings|META");

    let entity = listing_to_entity(&record);
    assert_eq!(entity["PartitionKey"].as_str(), Some("listings|META"));
    assert_eq!(entity["RowKey"].as_str(), Some("FB"));
    assert_eq!(entity["FinalPrice"].as_str(), Some("196.35"));
    assert_eq!(listing_from_entity(&entity), Ok(record));

    let active = successor();
    assert_eq!(listing_from_entity(&listing_to_entity(&active)), Ok(active));
}

#[test]
fn azure_adapters_round_trip_without_live_azure() {
    let table = Arc::new(FakeTable::default());
    let repository =
        AzureTableCatalogRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    let record = coverage(CoverageStatus::Error);
    repository.upsert(&record).expect("upsert");
    assert_eq!(
        repository
            .get(
                AssetClass::Equity,
                DataType::Trade,
                "AAPL",
                record.session_date
            )
            .expect("get"),
        Some(record)
    );

    let jobs = AzureTableJobRepository::new(Arc::new(FakeTable::default()));
    let record = job(JobState::Failed);
    jobs.upsert(&record).expect("upsert");
    assert_eq!(jobs.get(&record.job_id).expect("get"), Some(record));

    let contracts = AzureTableContractRepository::new(Arc::new(FakeTable::default()));
    let record = contract();
    contracts.upsert(&record).expect("upsert");
    let symbol = record.symbol.clone();
    assert_eq!(
        contracts
            .get(&table_key("AAPL240119C00150000"))
            .expect("get"),
        Some(record.clone())
    );
    assert_eq!(contracts.get(&symbol).expect("get"), Some(record));
}

#[test]
fn azure_adapters_store_the_expected_partition_and_row_keys() {
    let table = Arc::new(FakeTable::default());
    AzureTableCatalogRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>)
        .upsert(&coverage(CoverageStatus::Error))
        .expect("upsert");
    assert!(table.entities().contains_key(&(
        "coverage|equity|trade|AAPL".to_string(),
        "2024-01-19".to_string()
    )));

    let table = Arc::new(FakeTable::default());
    AzureTableJobRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>)
        .upsert(&job(JobState::Failed))
        .expect("upsert");
    assert!(table
        .entities()
        .contains_key(&("jobs".to_string(), "job%2F2024-01-19".to_string())));

    let table = Arc::new(FakeTable::default());
    AzureTableContractRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>)
        .upsert(&contract())
        .expect("upsert");
    assert!(table
        .entities()
        .contains_key(&("contracts".to_string(), "AAPL240119C00150000".to_string())));
}

#[test]
fn azure_catalog_uses_partition_query_and_deterministic_date_filter() {
    let table = Arc::new(FakeTable::default());
    let repository =
        AzureTableCatalogRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    repository
        .upsert(&coverage(CoverageStatus::Complete))
        .expect("upsert");

    let records = repository
        .list(&CoverageQuery {
            asset_class: Some(AssetClass::Equity),
            data_type: Some(DataType::Trade),
            symbol: Some("AAPL".to_string()),
            start: Some(Date::from_ymd(2024, 1, 1).expect("start")),
            end: Some(Date::from_ymd(2024, 1, 31).expect("end")),
        })
        .expect("list");

    assert_eq!(records, vec![coverage(CoverageStatus::Complete)]);
    let queries = table.queries();
    assert_eq!(queries.len(), 1);
    assert_eq!(queries[0].0, "PartitionKey eq @partition");
    assert_eq!(
        queries[0].1.get("partition").map(String::as_str),
        Some("coverage|equity|trade|AAPL")
    );
}

#[test]
fn azure_catalog_unqualified_list_uses_partition_range() {
    let table = Arc::new(FakeTable::default());
    let repository =
        AzureTableCatalogRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    repository
        .upsert(&coverage(CoverageStatus::Complete))
        .expect("upsert");

    let records = repository.list(&CoverageQuery::default()).expect("list");
    assert_eq!(records, vec![coverage(CoverageStatus::Complete)]);
    let queries = table.queries();
    assert_eq!(
        queries[0].0,
        "PartitionKey ge @start and PartitionKey lt @end"
    );
    assert_eq!(
        queries[0].1.get("start").map(String::as_str),
        Some("coverage|")
    );
    assert_eq!(
        queries[0].1.get("end").map(String::as_str),
        Some("coverage}")
    );
}

#[test]
fn azure_job_and_contract_lists_filter_after_table_scan() {
    let table = Arc::new(FakeTable::default());
    let jobs = AzureTableJobRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    let contracts = AzureTableContractRepository::new(Arc::new(FakeTable::default()));
    jobs.upsert(&job(JobState::Complete)).expect("upsert");
    contracts.upsert(&contract()).expect("upsert");

    assert_eq!(
        jobs.list(Some(JobState::Complete)).expect("list"),
        vec![job(JobState::Complete)]
    );
    assert_eq!(
        contracts
            .list(Some("AAPL"), Some(session_date()))
            .expect("list"),
        vec![contract()]
    );
}

#[test]
fn azure_run_repository_round_trips_and_deletes() {
    let table = Arc::new(FakeTable::default());
    let adapter = AzureTableRunRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    let record = run(RunState::Failed);

    adapter.upsert(&record).expect("upsert");
    assert!(table
        .entities()
        .contains_key(&("runs".to_string(), "run-2024-01-02".to_string())));
    assert_eq!(
        adapter.get(&record.run_id).expect("get"),
        Some(record.clone())
    );
    assert_eq!(
        adapter.list(Some(RunState::Failed), None).expect("list"),
        vec![record.clone()]
    );
    assert_eq!(
        adapter.list(Some(RunState::Complete), None).expect("list"),
        Vec::<RunRecord>::new()
    );
    assert_eq!(
        adapter.list(None, Some("momentum")).expect("list"),
        vec![record.clone()]
    );
    assert_eq!(
        adapter.list(None, Some("mean-revert")).expect("list"),
        Vec::<RunRecord>::new()
    );
    assert!(adapter.delete(&record.run_id).expect("delete"));
    assert!(!adapter.delete(&record.run_id).expect("delete again"));
    assert_eq!(adapter.get(&record.run_id).expect("get"), None);
}

#[test]
fn azure_run_repository_queries_only_the_runs_partition() {
    let table = Arc::new(FakeTable::default());
    let adapter = AzureTableRunRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    let record = run(RunState::Complete);
    adapter.upsert(&record).expect("upsert");

    assert_eq!(adapter.list(None, None).expect("list"), vec![record]);
    let queries = table.queries();
    assert_eq!(queries.len(), 1);
    assert_eq!(queries[0].0, "PartitionKey eq @partition");
    assert_eq!(
        queries[0].1.get("partition").map(String::as_str),
        Some("runs")
    );
    assert_eq!(adapter.get("missing-run").expect("get"), None);
}

#[test]
fn azure_run_entity_round_trips_all_states_with_metrics_json() {
    for status in RUN_STATES {
        let record = run(status);
        let entity = run_to_entity(&record);
        assert_eq!(
            entity["Metrics"].as_str(),
            Some(r#"{"max_drawdown":"-0.0825","sharpe":"1.2345"}"#)
        );
        assert_eq!(run_from_entity(&entity), Ok(record));
    }
}

#[test]
fn azure_listing_adapter_round_trips_without_live_azure() {
    let table = Arc::new(FakeTable::default());
    let adapter = AzureTableListingRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    let record = episode();

    adapter.upsert(&record).expect("upsert");
    assert!(table
        .entities()
        .contains_key(&("listings|META".to_string(), "FB".to_string())));
    assert_eq!(
        adapter.get("meta", "fb").expect("get"),
        Some(record.clone())
    );
    assert_eq!(adapter.get("META", "MISSING").expect("get"), None);
}

#[test]
fn azure_listing_adapter_uses_partition_query_and_post_filters() {
    let table = Arc::new(FakeTable::default());
    let adapter = AzureTableListingRepository::new(Arc::clone(&table) as Arc<dyn TableEntityStore>);
    adapter.upsert(&episode()).expect("upsert");
    adapter.upsert(&successor()).expect("upsert");

    let records = adapter
        .list(
            Some("META"),
            Some("FB"),
            Some(Date::from_ymd(2020, 1, 2).expect("as of")),
        )
        .expect("list");
    assert_eq!(records, vec![episode()]);
    let queries = table.queries();
    assert_eq!(queries.len(), 1);
    assert_eq!(queries[0].0, "PartitionKey eq @partition");
    assert_eq!(
        queries[0].1.get("partition").map(String::as_str),
        Some("listings|META")
    );

    let unqualified = AzureTableListingRepository::new(Arc::new(FakeTable::default()));
    unqualified.upsert(&episode()).expect("upsert");
    unqualified.upsert(&successor()).expect("upsert");

    let records = unqualified.list(None, None, None).expect("list");
    assert_eq!(
        records
            .iter()
            .map(|r| r.symbol.as_str())
            .collect::<Vec<_>>(),
        vec!["FB", "META"]
    );
    assert_eq!(
        unqualified.list(Some("MISSING"), None, None).expect("list"),
        Vec::<ListingRecord>::new()
    );
}

#[test]
fn metrics_json_is_canonical_sorted_and_ascii_escaped() {
    let mut metrics = BTreeMap::new();
    metrics.insert("sharpe".to_string(), "1.2345".to_string());
    metrics.insert("symbol".to_string(), "\u{3b1}".to_string());
    let text = super::metrics_json(&metrics);
    assert_eq!(text, r#"{"sharpe":"1.2345","symbol":"\u03B1"}"#);
}
