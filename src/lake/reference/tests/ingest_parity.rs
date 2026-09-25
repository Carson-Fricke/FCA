//! Ingest parity integration tests: the golden fixtures drive the whole
//! ingest, and the written partitions are asserted against the point-in-time
//! read contract (`tests/lake/reference/test_query.py` semantics) and against
//! the Python-written oracle Parquet in `tests/oracle/`.
//!
//! The oracle files are generated once with the Python writer settings
//! (`compression="zstd"`, `version="2.6"`, `use_compliant_nested_type=False`)
//! from `lake.reference.schemas` against the committed fixtures; they prove
//! Python-written partitions decode through the Rust-written schema.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_schema::Schema;
use lake_catalog::MemoryCatalogRepository;
use lake_contracts::{Date, DateTime, Decimal18, SCHEMA_METADATA_KEY, SCHEMA_VERSION_METADATA_KEY};
use lake_reference::ingest::stage;
use lake_reference::{
    classification_snapshot, fundamentals_facts, latest_fundamentals, ClassificationIngest,
    FactFilter, FactRow, ReferenceIngestCommitter, ReferenceObjectStore, XbrlFact,
    CLASSIFICATION_OBJECT_NAME,
};
use lake_storage::{BlobStore, LocalFilesystemStore};
use tempfile::TempDir;

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn oracle_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/oracle")
}

fn date(y: i32, m: u32, d: u32) -> Date {
    Date::from_ymd(y, m, d).expect("valid date")
}

fn decimal(text: &str) -> Decimal18 {
    Decimal18::parse(text).expect("valid decimal")
}

fn fixture_facts() -> Vec<XbrlFact> {
    let payload = std::fs::read(fixtures_dir().join("companyfacts.json")).expect("fixture");
    let document: serde_json::Value = serde_json::from_slice(&payload).expect("fixture json");
    lake_reference::parse_companyfacts(&document).expect("facts")
}

fn committer(root: &Path) -> ReferenceIngestCommitter {
    ReferenceIngestCommitter::new(
        Arc::new(LocalFilesystemStore::new(root)),
        Arc::new(MemoryCatalogRepository::new()),
    )
}

fn ingest_classification_fixture(root: &Path, snapshot_date: Option<Date>) -> Vec<String> {
    let payload = std::fs::read(fixtures_dir().join("nasdaqtraded.txt")).expect("fixture");
    let mut snapshot = lake_reference::parse_nasdaq_traded(&payload).expect("snapshot");
    if let Some(snapshot_date) = snapshot_date {
        snapshot.snapshot_date = snapshot_date;
    }
    let result = ClassificationIngest::new(committer(root))
        .run(&snapshot)
        .expect("ingest");
    result
        .objects
        .iter()
        .map(|item| item.path.clone())
        .collect()
}

#[test]
fn fixture_classification_snapshot_ingests_into_the_frozen_layout() {
    let root = TempDir::new().expect("root");
    let paths = ingest_classification_fixture(root.path(), None);

    assert_eq!(
        paths,
        vec![format!(
            "date=2024-04-19/asset_class=reference/data_type=classification/{CLASSIFICATION_OBJECT_NAME}.parquet"
        )]
    );

    let rows = classification_snapshot(root.path(), date(2024, 4, 19)).expect("rows");
    assert_eq!(
        rows.iter()
            .map(|row| row.symbol.as_str())
            .collect::<Vec<_>>(),
        ["AAPL", "GOOG", "MSFT", "ZVZZT"]
    );
    assert_eq!(rows[0].security_name, "Apple Inc. Common Stock");
    assert_eq!(
        rows[0].file_creation_time,
        DateTime::from_ymd_hms(2024, 4, 19, 21, 20, 0, 0).expect("utc")
    );
    assert!(!rows[0].etf);
    assert!(!rows[0].test_issue);
    assert!(rows[3].test_issue);
    assert_eq!(rows[3].financial_status, None);
    assert_eq!(rows[0].round_lot_size, decimal("100"));
    assert_eq!(rows[0].listing_exchange, "Q");
    assert_eq!(rows[0].market_category, "Q");
}

#[test]
fn fixture_fundamentals_ingest_writes_per_cik_per_filed_partitions() {
    let root = TempDir::new().expect("root");
    let store = Arc::new(LocalFilesystemStore::new(root.path()));
    let facts = fixture_facts();

    let result = lake_reference::FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        Arc::new(MemoryCatalogRepository::new()),
    ))
    .run(&facts)
    .expect("ingest");

    assert_eq!(result.ciks, vec![320193]);
    let object_name = lake_reference::cik_object_name(320193).expect("name");
    let paths: Vec<String> = result
        .objects
        .iter()
        .map(|item| item.path.clone())
        .collect();
    assert_eq!(
        paths,
        vec![
            format!("date=2023-11-03/asset_class=reference/data_type=fundamentals/{object_name}.parquet"),
            format!("date=2024-02-02/asset_class=reference/data_type=fundamentals/{object_name}.parquet"),
            format!("date=2024-05-03/asset_class=reference/data_type=fundamentals/{object_name}.parquet"),
        ]
    );
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| (record.symbol.clone(), record.session_date.isoformat()))
            .collect::<Vec<_>>(),
        vec![
            ("0000320193".to_string(), "2023-11-03".to_string()),
            ("0000320193".to_string(), "2024-02-02".to_string()),
            ("0000320193".to_string(), "2024-05-03".to_string()),
        ]
    );

    // The written Parquet carries the pinned schema: metadata, field order,
    // types, and nullability identical to the Python schemas module.
    let stored = store
        .read_optional(&paths[0])
        .expect("read")
        .expect("object");
    let builder = parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(
        bytes::Bytes::from(stored),
    )
    .expect("reader");
    assert_eq!(
        builder.schema().metadata().get(SCHEMA_METADATA_KEY),
        Some(&"reference.fundamentals".to_string())
    );
    assert_eq!(
        builder.schema().metadata().get(SCHEMA_VERSION_METADATA_KEY),
        Some(&lake_reference::REFERENCE_SCHEMA_VERSION.to_string())
    );
    let expected = lake_reference::fundamentals_schema();
    for (actual, expected) in builder.schema().fields().iter().zip(expected.fields()) {
        assert_eq!(actual.name(), expected.name());
        assert_eq!(actual.data_type(), expected.data_type());
        assert_eq!(actual.is_nullable(), expected.is_nullable());
    }

    let rows =
        fundamentals_facts(root.path(), date(2024, 5, 3), FactFilter::none()).expect("facts");
    assert_eq!(rows.len(), 4);
    let shares = &rows[0];
    assert_eq!(shares.taxonomy, "dei");
    assert_eq!(shares.tag, "EntityCommonStockSharesOutstanding");
    assert_eq!(shares.unit, "shares");
    assert_eq!(shares.val, decimal("15464661000"));
    assert_eq!(shares.frame.as_deref(), Some("CY2023Q3I"));
    let revenues: Vec<&FactRow> = rows.iter().filter(|row| row.tag == "Revenues").collect();
    assert_eq!(
        revenues
            .iter()
            .map(|row| (row.accn.clone(), row.filed.isoformat()))
            .collect::<Vec<_>>(),
        [
            ("0000320193-24-000006".to_string(), "2024-02-02".to_string()),
            ("0000320193-24-000008".to_string(), "2024-05-03".to_string()),
        ]
    );
}

#[test]
fn fundamentals_ingest_is_idempotent_byte_for_byte() {
    let root = TempDir::new().expect("root");
    let store = Arc::new(LocalFilesystemStore::new(root.path()));
    let facts = fixture_facts();

    let first = lake_reference::FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        Arc::new(MemoryCatalogRepository::new()),
    ))
    .run(&facts)
    .expect("first");
    let first_bytes = object_bytes(root.path(), &first.objects);

    let second = lake_reference::FundamentalsIngest::new(ReferenceIngestCommitter::new(
        store.clone(),
        Arc::new(MemoryCatalogRepository::new()),
    ))
    .run(&facts)
    .expect("second");
    let second_bytes = object_bytes(root.path(), &second.objects);

    assert_eq!(first.objects, second.objects);
    assert_eq!(first.coverage, second.coverage);
    assert_eq!(first_bytes, second_bytes);
}

fn object_bytes(root: &Path, objects: &[lake_storage::ParquetMetadata]) -> Vec<(String, Vec<u8>)> {
    objects
        .iter()
        .map(|item| {
            let mut target = root.to_path_buf();
            for part in item.path.split('/') {
                target.push(part);
            }
            (item.path.clone(), std::fs::read(&target).expect("object"))
        })
        .collect()
}

#[test]
fn classification_replaces_whole_snapshots_and_serves_the_latest_known() {
    let root = TempDir::new().expect("root");
    ingest_classification_fixture(root.path(), Some(date(2024, 4, 18)));
    ingest_classification_fixture(root.path(), Some(date(2024, 4, 19)));

    let latest = classification_snapshot(root.path(), date(2024, 4, 19)).expect("rows");
    assert_eq!(latest.len(), 4);

    let previous = classification_snapshot(root.path(), date(2024, 4, 18)).expect("rows");
    assert_eq!(previous.len(), 4);

    let before_any = classification_snapshot(root.path(), date(2024, 4, 17));
    assert!(before_any.expect("rows").is_empty());
}

fn fact(filed: Date, val: &str, tag: &str, accn: &str) -> XbrlFact {
    XbrlFact {
        cik: 320193,
        taxonomy: "us-gaap".to_string(),
        tag: tag.to_string(),
        unit: "USD".to_string(),
        period_end: date(2023, 12, 30),
        fy: Some(2024),
        fp: Some("Q1".to_string()),
        form: "10-Q".to_string(),
        accn: accn.to_string(),
        filed,
        frame: None,
        val: decimal(val),
    }
}

fn fundamentals_committer(root: &TempDir) -> lake_reference::ReferenceIngestCommitter {
    lake_reference::ReferenceIngestCommitter::new(
        Arc::new(LocalFilesystemStore::new(root.path())),
        Arc::new(MemoryCatalogRepository::new()),
    )
}

fn run_fundamentals(root: &TempDir, facts: &[XbrlFact]) {
    lake_reference::FundamentalsIngest::new(fundamentals_committer(root))
        .run(facts)
        .expect("ingest");
}

#[test]
fn fundamentals_facts_exclude_facts_filed_after_the_as_of_instant() {
    let root = TempDir::new().expect("root");
    run_fundamentals(
        &root,
        &[
            fact(date(2024, 2, 2), "1", "Revenues", "0000320193-24-000006"),
            fact(date(2024, 5, 3), "2", "Revenues", "0000320193-24-000008"),
            fact(date(2023, 11, 3), "3", "Assets", "0000320193-23-000106"),
        ],
    );

    let visible =
        fundamentals_facts(root.path(), date(2024, 2, 2), FactFilter::none()).expect("facts");
    assert_eq!(
        visible
            .iter()
            .map(|row| (row.tag.as_str(), row.val))
            .collect::<Vec<_>>(),
        [("Assets", decimal("3")), ("Revenues", decimal("1"))]
    );

    let before_any =
        fundamentals_facts(root.path(), date(2024, 2, 1), FactFilter::none()).expect("facts");
    assert_eq!(
        before_any
            .iter()
            .map(|row| row.tag.as_str())
            .collect::<Vec<_>>(),
        ["Assets"]
    );

    let after_restatement =
        fundamentals_facts(root.path(), date(2024, 5, 3), FactFilter::none()).expect("facts");
    assert_eq!(
        after_restatement
            .iter()
            .map(|row| (row.tag.as_str(), row.val))
            .collect::<Vec<_>>(),
        [
            ("Assets", decimal("3")),
            ("Revenues", decimal("1")),
            ("Revenues", decimal("2")),
        ]
    );
}

#[test]
fn latest_fundamentals_resolves_restated_values_only_after_filing() {
    let root = TempDir::new().expect("root");
    run_fundamentals(
        &root,
        &[
            fact(date(2024, 2, 2), "1", "Revenues", "0000320193-24-000006"),
            fact(date(2024, 5, 3), "2", "Revenues", "0000320193-24-000008"),
        ],
    );

    let before =
        latest_fundamentals(root.path(), date(2024, 5, 2), FactFilter::none()).expect("latest");
    assert_eq!(before.len(), 1);
    assert_eq!(before[0].val, decimal("1"));
    assert_eq!(before[0].accn, "0000320193-24-000006");

    let on_filing =
        latest_fundamentals(root.path(), date(2024, 5, 3), FactFilter::none()).expect("latest");
    assert_eq!(on_filing.len(), 1);
    assert_eq!(on_filing[0].val, decimal("2"));
    assert_eq!(on_filing[0].accn, "0000320193-24-000008");
}

#[test]
fn partition_pruning_never_reads_later_knowledge_dates() {
    let root = TempDir::new().expect("root");
    run_fundamentals(
        &root,
        &[fact(
            date(2024, 2, 2),
            "1",
            "Revenues",
            "0000320193-24-000006",
        )],
    );

    let future = root
        .path()
        .join("date=2025-01-01")
        .join("asset_class=reference")
        .join("data_type=fundamentals")
        .join("CIK0000320193.parquet");
    std::fs::create_dir_all(future.parent().expect("parent")).expect("mkdir");
    std::fs::write(&future, b"corrupt-but-never-read").expect("write");

    let visible =
        fundamentals_facts(root.path(), date(2024, 5, 3), FactFilter::none()).expect("facts");
    assert_eq!(visible.len(), 1);
}

#[test]
fn row_level_filed_predicate_defends_mixed_knowledge_objects() {
    let root = TempDir::new().expect("root");
    let store = LocalFilesystemStore::new(root.path());
    let mixed_path =
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/CIK0000789019.parquet";
    let batch = lake_reference::fundamentals_table(vec![
        mixed_fact(2024, 2, 2, "1", "0000789019-24-000004"),
        mixed_fact(2024, 5, 3, "9", "0000789019-24-000009"),
    ])
    .expect("table");
    let staged = stage(&batch, mixed_path).expect("staged");
    BlobStore::replace(&store, &staged.metadata.path, &staged.payload).expect("replace");

    let visible = fundamentals_facts(
        root.path(),
        date(2024, 2, 2),
        FactFilter {
            cik: Some(789019),
            ..FactFilter::none()
        },
    )
    .expect("facts");
    assert_eq!(visible.len(), 1);
    assert_eq!(visible[0].val, decimal("1"));

    let later = fundamentals_facts(
        root.path(),
        date(2024, 5, 3),
        FactFilter {
            cik: Some(789019),
            ..FactFilter::none()
        },
    )
    .expect("facts");
    assert_eq!(later.len(), 2);
    assert_eq!(later[0].val, decimal("1"));
    assert_eq!(later[1].val, decimal("9"));
}

fn mixed_fact(year: i32, month: u32, day: u32, val: &str, accn: &str) -> lake_reference::FactRow {
    lake_reference::FactRow {
        cik: 789019,
        taxonomy: "us-gaap".to_string(),
        tag: "Revenues".to_string(),
        unit: "USD".to_string(),
        period_end: date(2023, 12, 30),
        fy: Some(2024),
        fp: Some("Q1".to_string()),
        form: "10-Q".to_string(),
        accn: accn.to_string(),
        filed: date(year, month, day),
        frame: None,
        val: decimal(val),
    }
}

#[test]
fn fact_filters_narrow_by_cik_taxonomy_tag_and_unit() {
    let root = TempDir::new().expect("root");
    run_fundamentals(&root, &fixture_facts());
    let as_of = date(2024, 5, 3);

    let assets = fundamentals_facts(
        root.path(),
        as_of,
        FactFilter {
            tag: Some("Assets"),
            ..FactFilter::none()
        },
    )
    .expect("facts");
    assert!(assets.iter().all(|row| row.tag == "Assets"));

    let cik = fundamentals_facts(
        root.path(),
        as_of,
        FactFilter {
            cik: Some(320193),
            ..FactFilter::none()
        },
    )
    .expect("facts");
    assert!(cik.iter().all(|row| row.cik == 320193));

    assert!(fundamentals_facts(
        root.path(),
        as_of,
        FactFilter {
            taxonomy: Some("nope"),
            ..FactFilter::none()
        },
    )
    .expect("facts")
    .is_empty());
    assert!(fundamentals_facts(
        root.path(),
        as_of,
        FactFilter {
            unit: Some("nope"),
            ..FactFilter::none()
        },
    )
    .expect("facts")
    .is_empty());
}

#[test]
fn unknown_roots_and_missing_dates_follow_the_contract() {
    let root = TempDir::new().expect("root");
    ingest_classification_fixture(root.path(), None);

    let missing = root.path().join("missing");
    assert!(
        fundamentals_facts(&missing, date(2024, 5, 3), FactFilter::none())
            .expect("facts")
            .is_empty()
    );
    assert!(classification_snapshot(&missing, date(2024, 4, 19))
        .expect("rows")
        .is_empty());
    assert!(classification_snapshot(root.path(), date(2024, 4, 17))
        .expect("rows")
        .is_empty());
}

#[test]
fn objects_with_unexpected_schemas_are_rejected_on_read() {
    let root = TempDir::new().expect("root");
    ingest_classification_fixture(root.path(), None);
    let foreign_schema = Schema::new(vec![arrow_schema::Field::new(
        "x",
        arrow_schema::DataType::Int64,
        false,
    )]);
    let batch = arrow_array::RecordBatch::new_empty(Arc::new(foreign_schema));
    let foreign = stage(&batch, "foreign.parquet").expect("staged");
    let foreign_path = root
        .path()
        .join("date=2023-11-03")
        .join("asset_class=reference")
        .join("data_type=fundamentals")
        .join("CIK0009999999.parquet");
    std::fs::create_dir_all(foreign_path.parent().expect("parent")).expect("mkdir");
    std::fs::write(&foreign_path, &foreign.payload).expect("write");

    let error = fundamentals_facts(root.path(), date(2024, 5, 3), FactFilter::none())
        .expect_err("rejected");
    assert!(error.to_string().contains("unexpected schema"));
}

#[test]
fn python_written_oracle_parquet_decodes_into_typed_rows() {
    let classification_payload =
        std::fs::read(oracle_dir().join("classification_snapshot.parquet")).expect("oracle");
    let rows = lake_reference::schemas::read_classification_rows(
        &classification_payload,
        "oracle/classification_snapshot.parquet",
        "oracle",
    )
    .expect("rows");
    assert_eq!(rows.len(), 2);
    assert_eq!(
        rows.iter()
            .map(|row| row.symbol.as_str())
            .collect::<Vec<_>>(),
        ["AAPL", "ZVZZT"]
    );
    assert_eq!(
        rows[0].file_creation_time,
        DateTime::from_ymd_hms(2024, 4, 19, 21, 20, 0, 0).expect("utc")
    );
    assert!(rows[1].test_issue);
    assert_eq!(rows[1].financial_status, None);
    assert_eq!(rows[0].round_lot_size, decimal("100"));

    let fundamentals_payload =
        std::fs::read(oracle_dir().join("fundamentals_cik0000320193.parquet")).expect("oracle");
    let facts = lake_reference::schemas::read_fundamentals_rows(
        &fundamentals_payload,
        "oracle/fundamentals_cik0000320193.parquet",
        "oracle",
    )
    .expect("facts");
    assert_eq!(facts.len(), 4);
    assert_eq!(facts[0].cik, 320193);
    assert_eq!(facts[0].taxonomy, "dei");
    assert_eq!(facts[0].tag, "EntityCommonStockSharesOutstanding");
    assert_eq!(facts[0].val, decimal("15464661000"));
    assert_eq!(facts[0].frame.as_deref(), Some("CY2023Q3I"));
    assert_eq!(facts[2].tag, "Revenues");
    assert_eq!(facts[2].filed, date(2024, 2, 2));
    assert_eq!(facts[2].val, decimal("119575000000"));
    assert_eq!(facts[3].tag, "Revenues");
    assert_eq!(facts[3].filed, date(2024, 5, 3));
    assert_eq!(facts[3].val, decimal("119999000000"));
    assert_eq!(facts[3].frame, None);

    // Rebuilding the tables from decoded rows must reproduce the pinned
    // schema metadata the Python writer embedded.
    let table = lake_reference::classification_table(rows).expect("table");
    assert_eq!(
        table.schema().metadata().get(SCHEMA_METADATA_KEY),
        Some(&"reference.classification".to_string())
    );
    assert_eq!(
        table.schema().metadata().get(SCHEMA_VERSION_METADATA_KEY),
        Some(&lake_reference::REFERENCE_SCHEMA_VERSION.to_string())
    );
    let facts_table = lake_reference::fundamentals_table(facts).expect("table");
    assert_eq!(
        facts_table.schema().metadata().get(SCHEMA_METADATA_KEY),
        Some(&"reference.fundamentals".to_string())
    );
    // And the decoded Python-written schema must equal the Rust-pinned schema
    // field-for-field.
    let expected = lake_reference::fundamentals_schema();
    for (actual, expected) in facts_table.schema().fields().iter().zip(expected.fields()) {
        assert_eq!(actual.name(), expected.name());
        assert_eq!(actual.data_type(), expected.data_type());
        assert_eq!(actual.is_nullable(), expected.is_nullable());
    }
}

#[test]
fn cik_symbols_round_trip_between_symbols_and_paths() {
    assert_eq!(
        lake_reference::cik_symbol(320193).expect("symbol"),
        "0000320193"
    );
    assert_eq!(
        lake_reference::parse_cik_symbol("0000320193").expect("cik"),
        320193
    );
    assert_eq!(
        lake_reference::reference_partition_path(
            date(2024, 2, 2),
            lake_contracts::DataType::Fundamentals,
            &lake_reference::cik_object_name(320193).expect("name"),
        )
        .expect("path"),
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/CIK0000320193.parquet"
    );
}
