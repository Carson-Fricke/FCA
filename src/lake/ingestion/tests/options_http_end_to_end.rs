//! End-to-end coverage of one real option ingest transaction served by the
//! published HTTP option coverage route.
//!
//! This exercises the composition the future queue worker will drive: a real
//! [`OptionIngestTransaction`] stages option Parquet through the real
//! partition writer and commits objects into a local filesystem store and
//! coverage rows into a real local catalog store; the option coverage route
//! then serves that catalog through the existing `lake-http` constructors.
//! `lake-http`'s own test target needs `lake-ingestion`/`lake-storage`
//! dev-dependencies before this test can move to `src/lake/http/tests/`,
//! which is owned by the integration step.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use http_body_util::BodyExt;
use lake_catalog::{
    CatalogRepository, ContractsCatalog, CoverageRecord, DailyFrontSetRepository, JsonCatalogStore,
    MemoryContractRepository, MemoryDailyFrontSetRepository,
};
use lake_configuration::UniverseSettings;
use lake_contracts::{DataError, Date, DateTime, Decimal18};
use lake_http::{create_app, LakeApiConfig, LakeApiState};
use lake_ingestion::{
    plan_option_backfill, AtomicIngestCommitter, OptionBackfillRequest, OptionChainSelector,
    OptionIngestResult, OptionIngestTransaction, OptionSpotProvider, PartitionWriter, SpotValues,
    StagedParquetObject, StagingPartitionWriter,
};
use lake_storage::{BlobStore, LocalFilesystemStore, ReadableBlobStore};
use serde_json::{json, Value};

mod support;

use support::{
    day, option_quote, option_session, option_trade, spy_option_contract, FakeOptionProvider,
};
use tower::ServiceExt;

const TOKEN: &str = "test-secret";

fn universes() -> UniverseSettings {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    UniverseSettings::load(Some(&root), &BTreeMap::new()).expect("universes.yaml")
}

/// A fixed spot provider: every session uses SPY at exactly 500.
struct FixedSpot;

impl OptionSpotProvider for FixedSpot {
    fn spot(&self, _underlying: &str, _selection_date: Date) -> Result<Decimal18, DataError> {
        Decimal18::parse("500").map_err(DataError::from)
    }
}

/// Committer publishing objects to the local store and coverage to the shared
/// catalog — the same shape the local launcher's committer provides.
struct LocalOptionCommitter {
    store: Arc<LocalFilesystemStore>,
    catalog: Arc<JsonCatalogStore>,
    commits: Mutex<Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)>>,
}

impl LocalOptionCommitter {
    fn new(store: Arc<LocalFilesystemStore>, catalog: Arc<JsonCatalogStore>) -> Self {
        Self {
            store,
            catalog,
            commits: Mutex::new(Vec::new()),
        }
    }

    fn commit_count(&self) -> usize {
        self.commits.lock().expect("commits").len()
    }
}

impl AtomicIngestCommitter for LocalOptionCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        for object in objects {
            self.store.replace(&object.metadata.path, &object.payload)?;
        }
        for record in coverage {
            self.catalog.upsert(record)?;
        }
        self.commits
            .lock()
            .expect("commits")
            .push((objects.to_vec(), coverage.to_vec()));
        Ok(())
    }
}

async fn send(router: &axum::Router, path: &str) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("GET")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {TOKEN}"))
        .body(Body::empty())
        .expect("request");
    let response = router.clone().oneshot(request).await.expect("oneshot");
    let status = response.status();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, body)
}

fn instant(hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 22, hour, minute, 0, 0).expect("instant")
}

#[tokio::test]
async fn option_transactions_publish_and_the_coverage_route_serves_them() {
    let temp = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(LocalFilesystemStore::new(temp.path().join("objects")));
    let catalog =
        Arc::new(JsonCatalogStore::open(temp.path().join("catalog.json")).expect("catalog"));
    let contracts = Arc::new(MemoryContractRepository::from_records([
        spy_option_contract(),
    ]));
    let front_sets = Arc::new(MemoryDailyFrontSetRepository::new());
    let option_catalog = ContractsCatalog::new(
        Arc::clone(&contracts) as Arc<dyn lake_catalog::ContractRepository>,
        Arc::clone(&front_sets) as Arc<dyn lake_catalog::DailyFrontSetRepository>,
    );
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");

    // The planner only plans sessions whose front set is non-empty: the one
    // fixture contract expires 2024-01-19, so the 2024-01-22 session has no
    // selectable contracts and stays out of the plan. The transactions below
    // exercise both sessions directly, bypassing the front-set filter.
    let plan = plan_option_backfill(
        &option_catalog,
        catalog.as_ref(),
        &selector,
        OptionBackfillRequest {
            underlyings: Some(&["SPY".to_string()]),
            universe: None,
            spots: SpotValues::Provider(Arc::new(FixedSpot)),
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 22),
            as_of: Some(DateTime::from_ymd_hms(2024, 1, 23, 0, 0, 0, 0).expect("as of")),
            clock: None,
            max_items: None,
        },
    )
    .expect("plan");
    assert_eq!(plan.session_dates(), vec![day(2024, 1, 19)]);

    // Session one is a closed session with no provider rows: it must write
    // valid empty parquet and terminal `empty` coverage. Session two carries
    // one trade and one quote.
    let closed_session = option_session(false);
    let empty_transaction = OptionIngestTransaction::new(
        Arc::new(FakeOptionProvider::new(Vec::new(), Vec::new())),
        Arc::new(StagingPartitionWriter::new()) as Arc<dyn PartitionWriter>,
        Arc::new(LocalOptionCommitter::new(
            Arc::clone(&store),
            Arc::clone(&catalog),
        )),
    );
    let empty_result: OptionIngestResult = empty_transaction
        .run(spy_option_contract(), &closed_session, None)
        .await
        .expect("empty transaction");
    assert_eq!(
        empty_result
            .coverage
            .iter()
            .map(|record| record.status.value())
            .collect::<Vec<_>>(),
        vec!["empty", "empty"]
    );
    assert_eq!(
        empty_result
            .objects
            .iter()
            .map(|metadata| metadata.path.as_str())
            .collect::<Vec<_>>(),
        vec![
            "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet",
            "date=2024-01-19/asset_class=option/data_type=quote/SPY240119C00500000.parquet",
        ]
    );

    let complete_session = lake_coverage::CoverageSession {
        session_date: day(2024, 1, 22),
        open_time: DateTime::from_ymd_hms(2024, 1, 22, 14, 30, 0, 0).expect("open"),
        close_time: DateTime::from_ymd_hms(2024, 1, 22, 21, 0, 0, 0).expect("close"),
        is_partial: false,
    };
    let committer = Arc::new(LocalOptionCommitter::new(
        Arc::clone(&store),
        Arc::clone(&catalog),
    ));
    let complete_transaction = OptionIngestTransaction::new(
        Arc::new(FakeOptionProvider::new(
            vec![option_trade(instant(14, 31), "SPY240119C00500000")],
            vec![option_quote(instant(14, 32), "SPY240119C00500000")],
        )),
        Arc::new(StagingPartitionWriter::new()) as Arc<dyn PartitionWriter>,
        Arc::clone(&committer) as Arc<dyn AtomicIngestCommitter>,
    );
    let complete_result = complete_transaction
        .run(spy_option_contract(), &complete_session, None)
        .await
        .expect("complete transaction");
    assert_eq!(
        complete_result
            .coverage
            .iter()
            .map(|record| record.status.value())
            .collect::<Vec<_>>(),
        vec!["complete", "complete"]
    );

    // The committed objects are on disk with the exact frozen Hive paths, and
    // each is a decodable non-empty parquet payload.
    for path in [
        "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet",
        "date=2024-01-19/asset_class=option/data_type=quote/SPY240119C00500000.parquet",
        "date=2024-01-22/asset_class=option/data_type=trade/SPY240119C00500000.parquet",
        "date=2024-01-22/asset_class=option/data_type=quote/SPY240119C00500000.parquet",
    ] {
        let payload = store.read(path).expect("object exists");
        assert!(payload.len() > 100, "{path} must be a valid parquet object");
    }
    assert_eq!(committer.commit_count(), 1);

    // The published option coverage route serves the shared local catalog and
    // selects contracts from the persisted daily front sets: the plan
    // persisted the 2024-01-19 front set, and the 2024-01-22 one is inserted
    // the way a backfill with a longer-dated contract would persist it.
    front_sets
        .upsert(
            &lake_catalog::DailyFrontSet::new("SPY", day(2024, 1, 22), vec![spy_option_contract()])
                .expect("front set"),
        )
        .expect("persist front set");
    let as_of = DateTime::from_ymd_hms(2024, 1, 23, 0, 0, 0, 0).expect("clock");
    let state = LakeApiState {
        catalog: Arc::clone(&catalog) as Arc<dyn CatalogRepository>,
        jobs: Arc::new(lake_catalog::MemoryJobRepository::new()),
        listings: None,
        contracts: Some(Arc::clone(&contracts) as Arc<dyn lake_catalog::ContractRepository>),
        front_sets: Some(Arc::clone(&front_sets) as Arc<dyn lake_catalog::DailyFrontSetRepository>),
        sas_issuer: None,
        ensure_launcher: None,
        ensure_callback: None,
        bounded_query: None,
        authorized_force_override: false,
        ensure_wait_deadline_seconds: 1.0,
        bearer_token: Some(TOKEN.to_string()),
        now: Arc::new(move || as_of),
    };
    let router = create_app(
        Arc::new(state),
        LakeApiConfig {
            bearer_token: Some(TOKEN.to_string()),
            ensure_wait_deadline_seconds: 1.0,
        },
    );

    let (status, body) = send(
        &router,
        "/v1/catalog/coverage?asset_class=option&data_type=trade&underlying=spy&start_date=2024-01-19&end_date=2024-01-22",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["asset_class"], "option");
    assert_eq!(body["underlying"], "SPY");
    assert_eq!(body["contracts"], json!(["SPY240119C00500000"]));
    assert_eq!(body["entries"].as_array().expect("entries").len(), 2);
    assert_eq!(body["entries"][0]["session_date"], "2024-01-19");
    assert_eq!(body["entries"][0]["status"], "empty");
    assert_eq!(
        body["entries"][0]["object_path"],
        "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet"
    );
    assert_eq!(body["entries"][1]["session_date"], "2024-01-22");
    assert_eq!(body["entries"][1]["status"], "complete");
    assert_eq!(body["entries"][1]["row_count"], 1);
    assert_eq!(
        body["entries"][1]["object_path"],
        "date=2024-01-22/asset_class=option/data_type=trade/SPY240119C00500000.parquet"
    );

    let (status, body) = send(
        &router,
        "/v1/catalog/coverage?asset_class=option&data_type=quote&underlying=spy&start_date=2024-01-19&end_date=2024-01-22",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["entries"][0]["status"], "empty");
    assert_eq!(body["entries"][1]["status"], "complete");
}
