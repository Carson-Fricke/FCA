use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use lake_catalog::{CatalogRepository, CoverageStatus, MemoryCatalogRepository};
use lake_contracts::{Date, Decimal18};
use serde_json::Value;

use super::*;
use crate::configuration::{ReferenceHttpSettings, ReferenceRetrySettings, ReferenceSettings};
use crate::edgar::parse_companyfacts_bytes;
use crate::nasdaq::NasdaqSecurity;
use crate::partitions::cik_object_name;

fn fixtures_dir() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn company_facts() -> Vec<XbrlFact> {
    let payload = std::fs::read(fixtures_dir().join("companyfacts.json")).expect("fixture");
    parse_companyfacts_bytes(&payload).expect("facts")
}

/// In-memory object store double recording the commit order.
#[derive(Default)]
struct FakeStore {
    events: Mutex<Vec<String>>,
    objects: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl FakeStore {
    fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    fn events(&self) -> Vec<String> {
        self.events.lock().expect("events").clone()
    }

    fn objects(&self) -> BTreeMap<String, Vec<u8>> {
        self.objects.lock().expect("objects").clone()
    }
}

impl ReferenceObjectStore for FakeStore {
    fn replace(
        &self,
        relative_path: &str,
        payload: &[u8],
    ) -> Result<(), lake_contracts::DataError> {
        self.events
            .lock()
            .expect("events")
            .push(format!("replace:{relative_path}"));
        self.objects
            .lock()
            .expect("objects")
            .insert(relative_path.to_string(), payload.to_vec());
        Ok(())
    }

    fn read_optional(
        &self,
        relative_path: &str,
    ) -> Result<Option<Vec<u8>>, lake_contracts::DataError> {
        Ok(self
            .objects
            .lock()
            .expect("objects")
            .get(relative_path)
            .cloned())
    }
}

/// Coverage sink double recording the commit order.
struct FakeCatalog {
    repository: MemoryCatalogRepository,
    events: Mutex<Vec<String>>,
    records: Mutex<Vec<CoverageRecord>>,
}

impl FakeCatalog {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            repository: MemoryCatalogRepository::new(),
            events: Mutex::new(Vec::new()),
            records: Mutex::new(Vec::new()),
        })
    }

    fn events(&self) -> Vec<String> {
        self.events.lock().expect("events").clone()
    }

    fn records(&self) -> Vec<CoverageRecord> {
        self.records.lock().expect("records").clone()
    }
}

impl ReferenceCoverageSink for FakeCatalog {
    fn upsert(&self, record: &CoverageRecord) -> Result<(), ReferenceError> {
        self.events
            .lock()
            .expect("events")
            .push(format!("coverage:{}", record.symbol));
        self.records.lock().expect("records").push(record.clone());
        CatalogRepository::upsert(&self.repository, record).map_err(ReferenceError::Data)
    }
}

fn snapshot() -> NasdaqSnapshot {
    NasdaqSnapshot {
        file_creation_time: eastern_instant(),
        snapshot_date: Date::from_ymd(2024, 4, 19).expect("date"),
        securities: vec![
            NasdaqSecurity {
                symbol: "AAPL".to_string(),
                security_name: "Apple Inc. Common Stock".to_string(),
                listing_exchange: "Q".to_string(),
                market_category: "Q".to_string(),
                etf: false,
                round_lot_size: Decimal18::parse("100").expect("lot"),
                test_issue: false,
                financial_status: Some("Q".to_string()),
            },
            NasdaqSecurity {
                symbol: "ZVZZT".to_string(),
                security_name: "ZVZZT Widget Test Security".to_string(),
                listing_exchange: "Z".to_string(),
                market_category: "Z".to_string(),
                etf: false,
                round_lot_size: Decimal18::parse("100").expect("lot"),
                test_issue: true,
                financial_status: None,
            },
        ],
    }
}

fn eastern_instant() -> crate::nasdaq::EasternInstant {
    // 17:20 US/Eastern on 2024-04-19 is 21:20 UTC.
    crate::nasdaq::parse_nasdaq_traded(b"Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\nY|A|A Security|Q|Q|N|100|N||A|A|N\nFile Creation Time|0419202417:20##\n")
        .expect("snapshot")
        .file_creation_time
}

fn settings() -> ReferenceSettings {
    ReferenceSettings {
        schema_version: 1,
        user_agent: "Test Company admin@test.example".to_string(),
        classification_url: "https://example.test/nasdaqtraded.txt".to_string(),
        company_facts_url_template: "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
            .to_string(),
        company_concept_url_template:
            "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/{taxonomy}/{tag}.json"
                .to_string(),
        bulk_company_facts_url: "https://www.sec.gov/Archives/edgar/companyfacts.zip".to_string(),
        http: ReferenceHttpSettings {
            requests_per_second: 10.0,
            timeout_seconds: 30,
            retry: ReferenceRetrySettings {
                max_attempts: 5,
                honor_retry_after: true,
                backoff_seconds: 0.5,
                max_backoff_seconds: 30.0,
            },
        },
    }
}

#[test]
fn classification_ingest_publishes_snapshot_and_coverage() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let path = "date=2024-04-19/asset_class=reference/data_type=classification/SNAPSHOT.parquet";

    let result = ClassificationIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        catalog.clone(),
    ))
    .run(&snapshot(), &CompanyTickers::default())
    .expect("ingest");

    assert_eq!(
        result.snapshot_date,
        Date::from_ymd(2024, 4, 19).expect("date")
    );
    assert_eq!(result.objects[0].path, path);
    assert_eq!(result.objects[0].rows, 2);
    assert_eq!(result.coverage[0].asset_class.value(), "reference");
    assert_eq!(result.coverage[0].data_type.value(), "classification");
    assert_eq!(result.coverage[0].symbol, "DIRECTORY");
    assert_eq!(result.coverage[0].status, CoverageStatus::Complete);
    assert_eq!(result.coverage[0].row_count, 2);
    assert_eq!(
        result.coverage[0].checksum,
        Some(result.objects[0].checksum.clone())
    );

    let objects = store.objects();
    let table =
        crate::schemas::read_classification_rows(&objects[path], path, "test").expect("table");
    let symbols: Vec<&str> = table.iter().map(|row| row.symbol.as_str()).collect();
    assert_eq!(symbols, ["AAPL", "ZVZZT"]);
    let test_issues: Vec<bool> = table.iter().map(|row| row.test_issue).collect();
    assert_eq!(test_issues, [false, true]);
    let mut events = store.events();
    events.extend(catalog.events());
    assert_eq!(
        events,
        vec![format!("replace:{path}"), "coverage:DIRECTORY".to_string(),]
    );
}

#[test]
fn classification_ingest_is_idempotent() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let ingest = ClassificationIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        catalog.clone(),
    ));

    let first = ingest
        .run(&snapshot(), &CompanyTickers::default())
        .expect("first");
    let first_bytes = store.objects();

    let second = ingest
        .run(&snapshot(), &CompanyTickers::default())
        .expect("second");

    assert_eq!(first.coverage, second.coverage);
    assert_eq!(first.objects, second.objects);
    assert_eq!(first_bytes, store.objects());
    assert_eq!(catalog.records().len(), 2);
    assert_eq!(catalog.records()[1].row_count, 2);
}

#[test]
fn classification_ingest_with_no_securities_reports_empty() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let mut empty = snapshot();
    empty.securities.clear();

    let result = ClassificationIngest::new(ReferenceIngestCommitter::new(store, catalog))
        .run(&empty, &CompanyTickers::default())
        .expect("ingest");

    assert_eq!(result.coverage[0].status, CoverageStatus::Empty);
    assert_eq!(result.coverage[0].row_count, 0);
}

#[test]
fn fundamentals_ingest_publishes_per_cik_per_filed_objects() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();

    let result = FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        catalog.clone(),
    ))
    .run(&company_facts())
    .expect("ingest");

    assert_eq!(result.ciks, vec![320193]);
    let paths: Vec<String> = result
        .objects
        .iter()
        .map(|item| item.path.clone())
        .collect();
    assert_eq!(
        paths,
        vec![
            format!(
                "date=2023-11-03/asset_class=reference/data_type=fundamentals/{}.parquet",
                cik_object_name(320193).expect("name")
            ),
            format!(
                "date=2024-02-02/asset_class=reference/data_type=fundamentals/{}.parquet",
                cik_object_name(320193).expect("name"),
            ),
            format!(
                "date=2024-05-03/asset_class=reference/data_type=fundamentals/{}.parquet",
                cik_object_name(320193).expect("name"),
            ),
        ]
    );
    let keys: Vec<(String, String)> = result
        .coverage
        .iter()
        .map(|record| (record.symbol.clone(), record.session_date.isoformat()))
        .collect();
    assert_eq!(
        keys,
        vec![
            ("0000320193".to_string(), "2023-11-03".to_string()),
            ("0000320193".to_string(), "2024-02-02".to_string()),
            ("0000320193".to_string(), "2024-05-03".to_string()),
        ]
    );
    let objects = store.objects();
    let rows = crate::schemas::read_fundamentals_rows(&objects[&paths[0]], &paths[0], "test")
        .expect("rows");
    let tags: std::collections::HashSet<(String, String)> = rows
        .iter()
        .map(|row| (row.taxonomy.clone(), row.tag.clone()))
        .collect();
    assert_eq!(
        tags,
        [
            (
                "dei".to_string(),
                "EntityCommonStockSharesOutstanding".to_string()
            ),
            ("us-gaap".to_string(), "Assets".to_string()),
        ]
        .into()
    );
}

#[test]
fn fundamentals_ingest_is_idempotent() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let ingest = FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        catalog.clone(),
    ));

    let facts = company_facts();
    let first = ingest.run(&facts).expect("first");
    let first_bytes = store.objects();

    let second = ingest.run(&facts).expect("second");

    assert_eq!(first.coverage, second.coverage);
    assert_eq!(first.objects, second.objects);
    assert_eq!(first_bytes, store.objects());
}

#[test]
fn fundamentals_ingest_merges_other_tags_into_the_same_knowledge_object() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let ingest = FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        catalog.clone(),
    ));
    let path = "date=2024-02-02/asset_class=reference/data_type=fundamentals/CIK0000320193.parquet";
    let facts = company_facts();
    let revenues: Vec<XbrlFact> = facts
        .iter()
        .filter(|fact| fact.filed == Date::from_ymd(2024, 2, 2).expect("date"))
        .cloned()
        .collect();
    let assets: Vec<XbrlFact> = revenues
        .iter()
        .map(|fact| XbrlFact {
            tag: "Assets".to_string(),
            ..fact.clone()
        })
        .collect();

    ingest.run(&revenues).expect("revenues");
    ingest.run(&assets).expect("assets");
    let objects = store.objects();
    let rows = crate::schemas::read_fundamentals_rows(&objects[&path.to_string()], path, "test")
        .expect("rows");
    let tags: std::collections::HashSet<String> = rows.iter().map(|row| row.tag.clone()).collect();
    assert_eq!(tags, ["Revenues".to_string(), "Assets".to_string()].into());

    ingest.run(&revenues).expect("revenues again");
    let objects = store.objects();
    let rows = crate::schemas::read_fundamentals_rows(&objects[&path.to_string()], path, "test")
        .expect("rows");
    assert_eq!(rows.len(), 2);
}

#[test]
fn facts_sequence_is_sorted_and_deduplicated_before_staging() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let mut facts = company_facts();
    facts.extend(facts.clone());
    facts.reverse();

    let result = FundamentalsIngest::new(ReferenceIngestCommitter::new(store, catalog))
        .run(&facts)
        .expect("ingest");

    assert_eq!(result.objects[0].rows, 2);
}

#[test]
fn committer_replaces_objects_before_replacing_coverage() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();

    FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        catalog.clone(),
    ))
    .run(&company_facts())
    .expect("ingest");

    let mut events = store.events();
    events.extend(catalog.events());
    assert!(events[0].starts_with("replace:"));
    assert!(events[events.len() - 1].starts_with("coverage:"));
}

#[test]
fn existing_object_with_unexpected_schema_is_rejected() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();
    let path = "date=2024-02-02/asset_class=reference/data_type=fundamentals/CIK0000320193.parquet";
    let foreign_schema = arrow_schema::Schema::new(vec![arrow_schema::Field::new(
        "x",
        arrow_schema::DataType::Int64,
        false,
    )]);
    let batch = arrow_array::RecordBatch::new_empty(Arc::new(foreign_schema));
    let foreign = crate::ingest::stage(&batch, "foreign.parquet").expect("staged");
    store
        .objects
        .lock()
        .expect("objects")
        .insert(path.to_string(), foreign.payload);

    let error = FundamentalsIngest::new(ReferenceIngestCommitter::new(store, catalog))
        .run(&company_facts())
        .expect_err("rejected");
    assert!(error.to_string().contains("unexpected schema"));
}

#[test]
fn filter_facts_narrows_by_cik_and_tag() {
    let facts = company_facts();
    let other: Vec<XbrlFact> = facts
        .iter()
        .map(|fact| XbrlFact {
            cik: 789019,
            ..fact.clone()
        })
        .collect();
    let mut combined = facts.clone();
    combined.extend(other.clone());

    let unfiltered = filter_facts(&combined, None, None);
    assert_eq!(unfiltered, combined);
    let only_cik = filter_facts(&combined, Some(&[320193]), None);
    assert_eq!(only_cik, facts);
    let only_tag = filter_facts(&combined, None, Some(&["Revenues".to_string()][..]));
    assert!(only_tag.iter().all(|fact| fact.tag == "Revenues"));
    let both = filter_facts(
        &combined,
        Some(&[789019]),
        Some(&["Assets".to_string()][..]),
    );
    assert!(both
        .iter()
        .all(|fact| fact.cik == 789019 && fact.tag == "Assets"));
}

/// Offline stand-in for the fair-access client.
struct FakeClient {
    bytes_payloads: BTreeMap<String, Vec<u8>>,
    json_payloads: BTreeMap<String, Vec<u8>>,
    urls: Mutex<Vec<String>>,
}

impl FakeClient {
    fn bytes(bytes_payloads: BTreeMap<String, Vec<u8>>) -> Arc<Self> {
        Arc::new(Self {
            bytes_payloads,
            json_payloads: BTreeMap::new(),
            urls: Mutex::new(Vec::new()),
        })
    }

    fn json(json_payloads: BTreeMap<String, Vec<u8>>) -> Arc<Self> {
        Arc::new(Self {
            bytes_payloads: BTreeMap::new(),
            json_payloads,
            urls: Mutex::new(Vec::new()),
        })
    }

    fn urls(&self) -> Vec<String> {
        self.urls.lock().expect("urls").clone()
    }
}

impl ReferenceFetchClient for FakeClient {
    fn get_bytes<'a>(
        &'a self,
        url: &'a str,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Vec<u8>, ReferenceError>> + Send + 'a>,
    > {
        self.urls.lock().expect("urls").push(url.to_string());
        let payload = self.bytes_payloads.get(url).cloned();
        Box::pin(async move { payload.ok_or_else(|| ReferenceError::invalid("missing payload")) })
    }

    fn get_json<'a>(
        &'a self,
        url: &'a str,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Value, ReferenceError>> + Send + 'a>,
    > {
        self.urls.lock().expect("urls").push(url.to_string());
        let payload = self.json_payloads.get(url).cloned();
        Box::pin(async move {
            let bytes = payload.ok_or_else(|| ReferenceError::invalid("missing payload"))?;
            serde_json::from_slice(&bytes)
                .map_err(|error| ReferenceError::invalid(error.to_string()))
        })
    }
}

#[tokio::test]
async fn ingest_classification_uses_the_client_without_network() {
    let payload = std::fs::read(fixtures_dir().join("nasdaqtraded.txt")).expect("fixture");
    let tickers_payload =
        std::fs::read(fixtures_dir().join("company_tickers.json")).expect("fixture");
    let url = settings().classification_url.clone();
    let client = FakeClient::bytes(BTreeMap::from([
        (url.clone(), payload),
        (COMPANY_TICKERS_URL.to_string(), tickers_payload),
    ]));
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();

    let summary =
        ingest_classification(client.as_ref(), &settings(), store.clone(), catalog.clone())
            .await
            .expect("ingest");

    assert_eq!(client.urls(), vec![url, COMPANY_TICKERS_URL.to_string()]);
    assert_eq!(summary.snapshot_date, "2024-04-19");
    assert_eq!(summary.file_creation_time, "2024-04-19T17:20:00-04:00");
    assert_eq!(summary.coverage.len(), 1);
    assert_eq!(summary.coverage[0].rows, 4);
    assert_eq!(summary.coverage[0].session_date, "2024-04-19");
    assert_eq!(summary.coverage[0].status, "complete");
    assert_eq!(summary.coverage[0].symbol, "DIRECTORY");

    let objects = store.objects();
    let path = "date=2024-04-19/asset_class=reference/data_type=classification/SNAPSHOT.parquet";
    let rows = crate::schemas::read_classification_rows_with_extras(&objects[path], path, "test")
        .expect("rows");
    let joined: Vec<(String, Option<u64>)> = rows
        .iter()
        .map(|(row, extras)| (row.symbol.clone(), extras.cik))
        .collect();
    assert_eq!(
        joined,
        [
            ("AAPL".to_string(), Some(320193)),
            ("GOOG".to_string(), Some(1652044)),
            ("MSFT".to_string(), Some(789019)),
            ("ZVZZT".to_string(), None),
        ]
    );
    assert!(rows
        .iter()
        .all(|(_, extras)| extras.sic.is_none() && extras.sic_industry.is_none()));
}

#[tokio::test]
async fn ingest_fundamentals_increment_fetches_and_merges_without_network() {
    let payload = std::fs::read(fixtures_dir().join("companyfacts.json")).expect("fixture");
    let url = crate::edgar::company_facts_url(&settings().company_facts_url_template, 320193)
        .expect("url");
    let client = FakeClient::json(BTreeMap::from([(url, payload)]));
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();

    let summary = ingest_fundamentals_increment(
        client.as_ref(),
        &settings(),
        &[320193],
        None,
        store.clone(),
        catalog.clone(),
    )
    .await
    .expect("ingest");

    assert_eq!(
        client.urls(),
        vec!["https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json".to_string()]
    );
    assert_eq!(summary.ciks, vec![320193]);
    assert_eq!(
        summary
            .coverage
            .iter()
            .map(|entry| entry.session_date.clone())
            .collect::<Vec<_>>(),
        vec!["2023-11-03", "2024-02-02", "2024-05-03"]
    );
}

#[tokio::test]
async fn ingest_fundamentals_increment_tag_filter_limits_stored_rows() {
    let payload = std::fs::read(fixtures_dir().join("companyfacts.json")).expect("fixture");
    let url = crate::edgar::company_facts_url(&settings().company_facts_url_template, 320193)
        .expect("url");
    let client = FakeClient::json(BTreeMap::from([(url, payload)]));
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();

    let summary = ingest_fundamentals_increment(
        client.as_ref(),
        &settings(),
        &[320193],
        Some(&["Revenues".to_string()][..]),
        store.clone(),
        catalog.clone(),
    )
    .await
    .expect("ingest");

    assert_eq!(
        summary
            .coverage
            .iter()
            .map(|entry| entry.rows)
            .collect::<Vec<_>>(),
        vec![1, 1]
    );
    let stored = store.objects();
    let path = "date=2024-02-02/asset_class=reference/data_type=fundamentals/CIK0000320193.parquet";
    let rows = crate::schemas::read_fundamentals_rows(&stored[path], path, "test").expect("rows");
    assert_eq!(
        rows.iter().map(|row| row.tag.as_str()).collect::<Vec<_>>(),
        ["Revenues"]
    );
}

#[test]
fn summaries_render_the_python_shapes() {
    let store = FakeStore::new();
    let catalog = FakeCatalog::new();

    let result = ClassificationIngest::new(ReferenceIngestCommitter::new(store, catalog))
        .run(&snapshot(), &CompanyTickers::default())
        .expect("ingest");
    let summary = classification_summary(&result);

    assert_eq!(summary.snapshot_date, "2024-04-19");
    assert_eq!(summary.file_creation_time, "2024-04-19T17:20:00-04:00");
    assert_eq!(summary.objects.len(), 1);
    assert_eq!(summary.objects[0].rows, 2);
}
