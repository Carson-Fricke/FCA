//! Integration coverage for the option ingest transaction, backfill planner,
//! and queue handler.
//!
//! Ports `tests/lake/ingestion/options/test_transaction.py`: empty closed
//! sessions publish terminal `empty` coverage with valid parquet, later
//! deliveries replace the same paths, provider failures publish error
//! coverage without objects, the closing print at session close belongs to the
//! session, planning resumes from terminal coverage, and the queue payload
//! round-trips through the worker handle.

mod support;

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use lake_catalog::{
    CatalogRepository, CoverageQuery, CoverageRecord, CoverageStatus, DailyFrontSet,
};
use lake_configuration::UniverseSettings;
use lake_contracts::{DataError, DataType, Decimal18};
use lake_coverage::CoverageSession;
use lake_ingestion::{
    enqueue_option_backfill, plan_option_backfill, AtomicIngestCommitter, OptionBackfillPlan,
    OptionBackfillRequest, OptionChainSelector, OptionIngestTransaction, OptionQueueHandler,
    QueueSender, SpotValues, StagedParquetObject, StagingPartitionWriter,
};
use lake_providers::ProviderError;
use serde_json::{json, Value};
use support::{
    day, instant, option_quote, option_trade, spy_option_contract, FakeOptionProvider,
    RecordingCommitter, RecordingWriter,
};

fn universes() -> UniverseSettings {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    UniverseSettings::load(Some(&root), &BTreeMap::new()).expect("universes.yaml")
}

fn session() -> CoverageSession {
    CoverageSession {
        session_date: day(2024, 1, 19),
        open_time: instant(14, 30, 0, 0),
        close_time: instant(21, 0, 0, 0),
        is_partial: false,
    }
}

#[tokio::test]
async fn empty_option_session_is_terminal_and_replacement_is_deterministic() {
    let committer = Arc::new(RecordingCommitter::new());
    let provider = FakeOptionProvider::new(Vec::new(), Vec::new());
    let transaction = OptionIngestTransaction::new(
        Arc::new(provider),
        Arc::new(RecordingWriter::new()),
        committer.clone(),
    );

    let empty = transaction
        .run(spy_option_contract(), &session(), None)
        .await
        .expect("empty session");

    assert_eq!(
        empty
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Empty, CoverageStatus::Empty]
    );
    assert_eq!(
        empty
            .objects
            .iter()
            .map(|metadata| metadata.path.as_str())
            .collect::<Vec<_>>(),
        vec![
            "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet",
            "date=2024-01-19/asset_class=option/data_type=quote/SPY240119C00500000.parquet",
        ]
    );
    let first_bytes: Vec<u64> = empty.objects.iter().map(|object| object.bytes).collect();
    assert_eq!(committer.commit_count(), 1);

    let provider = FakeOptionProvider::new(
        vec![option_trade(instant(14, 31, 0, 0), "SPY240119C00500000")],
        vec![option_quote(instant(14, 32, 0, 0), "SPY240119C00500000")],
    );
    let transaction = OptionIngestTransaction::new(
        Arc::new(provider),
        Arc::new(RecordingWriter::new()),
        committer.clone(),
    );
    let complete = transaction
        .run(spy_option_contract(), &session(), None)
        .await
        .expect("complete session");

    assert_eq!(
        complete
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Complete, CoverageStatus::Complete]
    );
    assert_eq!(
        complete
            .objects
            .iter()
            .map(|metadata| metadata.path.as_str())
            .collect::<Vec<_>>(),
        empty
            .objects
            .iter()
            .map(|metadata| metadata.path.as_str())
            .collect::<Vec<_>>(),
        "a later delivery replaces the same paths"
    );
    assert_ne!(
        complete
            .objects
            .iter()
            .map(|object| object.bytes)
            .collect::<Vec<_>>(),
        first_bytes
    );
    assert_eq!(committer.commit_count(), 2);
}

#[tokio::test]
async fn option_provider_failure_publishes_error_coverage_without_objects() {
    let committer = Arc::new(RecordingCommitter::new());
    let writer = Arc::new(RecordingWriter::new());
    let provider = FakeOptionProvider::new(Vec::new(), Vec::new())
        .with_quote_error(ProviderError::Transport("OPRA unavailable".to_string()));
    let transaction = OptionIngestTransaction::new(Arc::new(provider), writer, committer.clone());

    let result = transaction
        .run(spy_option_contract(), &session(), None)
        .await
        .expect("error coverage published");

    assert!(result.objects.is_empty());
    assert!(
        result
            .coverage
            .iter()
            .all(|record| record.status == CoverageStatus::Error),
        "{:?}",
        result.coverage
    );
    assert!(
        result
            .coverage
            .iter()
            .all(|record| record.error_message.as_deref()
                == Some("quote: TransportError: OPRA unavailable")),
        "{:?}",
        result.coverage
    );
    assert_eq!(committer.commits()[0].0.len(), 0);
}

#[tokio::test]
async fn option_print_stamped_exactly_at_session_close_is_published() {
    // The fetch passes close_time as Alpaca's inclusive end, so the closing
    // auction print stamped exactly at 21:00:00Z belongs to the session on
    // both the trade and quote paths.
    let committer = Arc::new(RecordingCommitter::new());
    let provider = FakeOptionProvider::new(
        vec![option_trade(instant(21, 0, 0, 0), "SPY240119C00500000")],
        vec![option_quote(instant(21, 0, 0, 0), "SPY240119C00500000")],
    );
    let transaction = OptionIngestTransaction::new(
        Arc::new(provider),
        Arc::new(StagingPartitionWriter::new()),
        committer,
    );

    let result = transaction
        .run(spy_option_contract(), &session(), None)
        .await
        .expect("transaction");

    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Complete, CoverageStatus::Complete]
    );
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![1, 1]
    );
}

#[tokio::test]
async fn option_print_stamped_after_session_close_is_rejected() {
    let committer = Arc::new(RecordingCommitter::new());
    let writer = Arc::new(RecordingWriter::new());
    let provider = FakeOptionProvider::new(
        vec![option_trade(
            instant(21, 0, 0, 500_000),
            "SPY240119C00500000",
        )],
        vec![option_quote(instant(14, 31, 0, 0), "SPY240119C00500000")],
    );
    let transaction = OptionIngestTransaction::new(Arc::new(provider), writer, committer.clone());

    let result = transaction
        .run(spy_option_contract(), &session(), None)
        .await
        .expect("error coverage");

    assert!(result.objects.is_empty());
    assert!(
        committer.commits()[0].0.is_empty(),
        "no object is published for a rejected record"
    );
    assert!(
        result
            .coverage
            .iter()
            .all(|record| record.status == CoverageStatus::Error),
        "{:?}",
        result.coverage
    );
    assert!(
        result.coverage[0]
            .error_message
            .as_deref()
            .is_some_and(|message| message.contains("outside session")),
        "{:?}",
        result.coverage[0].error_message
    );
}

#[tokio::test]
async fn data_types_selection_orders_trade_then_quote() {
    let committer = Arc::new(RecordingCommitter::new());
    let writer = Arc::new(RecordingWriter::new());
    let provider = FakeOptionProvider::new(
        vec![option_trade(instant(14, 31, 0, 0), "SPY240119C00500000")],
        vec![option_quote(instant(14, 31, 0, 0), "SPY240119C00500000")],
    );
    let transaction = OptionIngestTransaction::new(Arc::new(provider), writer, committer.clone());

    let result = transaction
        .run(
            spy_option_contract(),
            &session(),
            Some(&[DataType::Quote, DataType::Trade]),
        )
        .await
        .expect("transaction");
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Trade, DataType::Quote],
        "publication order is canonical regardless of request order"
    );

    let result = transaction
        .run(spy_option_contract(), &session(), Some(&[DataType::Quote]))
        .await
        .expect("transaction");
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Quote]
    );
}

#[test]
fn option_planning_persists_daily_front_sets_and_resumes_terminal_coverage() {
    let contract = spy_option_contract();
    let catalog = lake_catalog::ContractsCatalog::new(
        Arc::new(lake_catalog::MemoryContractRepository::from_records([
            contract.clone(),
        ])),
        Arc::new(lake_catalog::MemoryDailyFrontSetRepository::new()),
    );
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");
    let spots = SpotValues::Map(BTreeMap::from([(
        "SPY".to_string(),
        Decimal18::parse("500").expect("spot"),
    )]));

    let first = plan(spots, &catalog, &coverage, &selector, None);
    assert_eq!(first.front_sets[0].symbols(), vec![contract.symbol.clone()]);
    assert_eq!(
        first.items[0].queue_message(),
        json!({
            "asset_class": "option",
            "data_types": ["trade", "quote"],
            "end_time": "2024-01-19T21:00:00Z",
            "expiration_date": "2024-01-19",
            "option_type": "call",
            "replace_existing": false,
            "session_date": "2024-01-19",
            "start_time": "2024-01-19T14:30:00Z",
            "strike_price": "500",
            "symbol": contract.symbol,
            "underlying": "SPY",
        })
    );
    assert_eq!(
        catalog
            .get_front_set("SPY", day(2024, 1, 19))
            .expect("front set"),
        Some(first.front_sets[0].clone())
    );

    for (data_type, status) in [
        (DataType::Trade, CoverageStatus::Empty),
        (DataType::Quote, CoverageStatus::Complete),
    ] {
        coverage
            .upsert(
                &CoverageRecord::new(
                    lake_contracts::AssetClass::Option,
                    data_type,
                    &contract.symbol,
                    day(2024, 1, 19),
                    status,
                    None,
                    0,
                    0,
                    None,
                    None,
                    None,
                )
                .expect("record"),
            )
            .expect("upsert");
    }

    let resumed = plan(
        SpotValues::Map(BTreeMap::from([(
            "SPY".to_string(),
            Decimal18::parse("500").expect("spot"),
        )])),
        &catalog,
        &coverage,
        &selector,
        None,
    );
    assert!(resumed.items.is_empty());
    assert!(!resumed.has_more());
    assert_eq!(resumed.front_sets, first.front_sets);
}

fn plan(
    spots: SpotValues,
    catalog: &lake_catalog::ContractsCatalog,
    coverage: &lake_catalog::MemoryCatalogRepository,
    selector: &OptionChainSelector,
    max_items: Option<usize>,
) -> OptionBackfillPlan {
    plan_option_backfill(
        catalog,
        coverage,
        selector,
        OptionBackfillRequest {
            underlyings: Some(&["SPY".to_string()]),
            universe: None,
            spots,
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 19),
            as_of: Some(instant_as_of()),
            clock: None,
            max_items,
        },
    )
    .expect("plan")
}

fn instant_as_of() -> lake_contracts::DateTime {
    lake_contracts::DateTime::from_ymd_hms(2024, 1, 20, 0, 0, 0, 0).expect("as of")
}

#[test]
fn option_plan_enqueue_uses_injected_sender() {
    let catalog = lake_catalog::ContractsCatalog::with_contracts(Arc::new(
        lake_catalog::MemoryContractRepository::from_records([spy_option_contract()]),
    ));
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");
    let plan = plan(
        SpotValues::Map(BTreeMap::from([(
            "SPY".to_string(),
            Decimal18::parse("500").expect("spot"),
        )])),
        &catalog,
        &coverage,
        &selector,
        None,
    );
    let messages: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));

    struct Sender(Arc<Mutex<Vec<Value>>>);
    impl QueueSender for Sender {
        fn send(&self, message: &Value) -> Result<(), DataError> {
            self.0.lock().expect("messages").push(message.clone());
            Ok(())
        }
    }

    let sent = enqueue_option_backfill(&plan, &Sender(Arc::clone(&messages))).expect("sent");
    assert_eq!(sent, 1);
    assert_eq!(
        *messages.lock().expect("messages"),
        vec![plan.items()[0].queue_message()]
    );
}

#[test]
fn option_planner_uses_injected_clock_and_rejects_two_time_sources() {
    let catalog = lake_catalog::ContractsCatalog::with_contracts(Arc::new(
        lake_catalog::MemoryContractRepository::from_records([spy_option_contract()]),
    ));
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");
    let clock: lake_ingestion::OptionClock = Arc::new(instant_as_of);
    let spots = SpotValues::Map(BTreeMap::from([(
        "SPY".to_string(),
        Decimal18::parse("500").expect("spot"),
    )]));

    let plan = plan_option_backfill(
        &catalog,
        &coverage,
        &selector,
        OptionBackfillRequest {
            underlyings: Some(&["SPY".to_string()]),
            universe: None,
            spots,
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 19),
            as_of: None,
            clock: Some(clock),
            max_items: None,
        },
    )
    .expect("plan");
    assert_eq!(plan.items().len(), 1);

    let error = plan_option_backfill(
        &catalog,
        &coverage,
        &selector,
        OptionBackfillRequest {
            underlyings: Some(&["SPY".to_string()]),
            universe: None,
            spots: SpotValues::Map(BTreeMap::new()),
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 19),
            as_of: Some(instant_as_of()),
            clock: Some(Arc::new(instant_as_of)),
            max_items: None,
        },
    )
    .expect_err("two time sources");
    assert!(error.to_string().contains("must not both"));
}

#[test]
fn option_planner_rejects_a_missing_spot_and_work_limits() {
    let catalog = lake_catalog::ContractsCatalog::with_contracts(Arc::new(
        lake_catalog::MemoryContractRepository::from_records([spy_option_contract()]),
    ));
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");

    let error = plan_option_backfill(
        &catalog,
        &coverage,
        &selector,
        OptionBackfillRequest {
            underlyings: Some(&["SPY".to_string()]),
            universe: None,
            spots: SpotValues::Map(BTreeMap::new()),
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 19),
            as_of: Some(instant_as_of()),
            clock: None,
            max_items: None,
        },
    )
    .expect_err("missing spot");
    assert_eq!(error.to_string(), "spot is missing for underlying SPY");

    let error = plan_option_backfill(
        &catalog,
        &coverage,
        &selector,
        OptionBackfillRequest {
            underlyings: None,
            universe: None,
            spots: SpotValues::Map(BTreeMap::new()),
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 19),
            as_of: Some(instant_as_of()),
            clock: None,
            max_items: None,
        },
    )
    .expect_err("no underlyings");
    assert_eq!(
        error.to_string(),
        "underlyings or universe must be supplied"
    );

    let error = plan_option_backfill(
        &catalog,
        &coverage,
        &selector,
        OptionBackfillRequest {
            underlyings: Some(&["SPY".to_string()]),
            universe: None,
            spots: SpotValues::Map(BTreeMap::from([(
                "SPY".to_string(),
                Decimal18::parse("500").expect("spot"),
            )])),
            data_types: None,
            start: day(2024, 1, 19),
            end: day(2024, 1, 19),
            as_of: Some(instant_as_of()),
            clock: None,
            max_items: Some(0),
        },
    )
    .expect_err("zero max_items");
    assert_eq!(error.to_string(), "max_items must be positive");
}

#[tokio::test]
async fn shared_queue_worker_composes_the_option_handler_idempotently() {
    // The queue worker crate is a later workstream; here the handler is driven
    // directly with the exact payload shape the Python test sent.
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let committer = Arc::new(LocalCommitter::new(Arc::new(coverage)));
    let front_sets = Arc::new(lake_catalog::MemoryDailyFrontSetRepository::from_records([
        DailyFrontSet::new("SPY", day(2024, 1, 19), vec![spy_option_contract()]).expect("set"),
    ]));
    let provider = FakeOptionProvider::new(
        vec![option_trade(instant(14, 31, 0, 0), "SPY240119C00500000")],
        vec![option_quote(instant(14, 32, 0, 0), "SPY240119C00500000")],
    );
    let transaction = OptionIngestTransaction::new(
        Arc::new(provider),
        Arc::new(StagingPartitionWriter::new()),
        Arc::clone(&committer) as Arc<dyn AtomicIngestCommitter>,
    );
    let handler = OptionQueueHandler::new(Arc::new(transaction), Some(front_sets));
    let payload = json!({
        "asset_class": "option",
        "data_types": ["trade", "quote"],
        "end_time": "2024-01-19T21:00:00Z",
        "expiration_date": "2024-01-19",
        "option_type": "call",
        "session_date": "2024-01-19",
        "start_time": "2024-01-19T14:30:00Z",
        "strike_price": "500",
        "symbol": "SPY240119C00500000",
        "underlying": "SPY",
    });

    handler.handle(&payload).await.expect("first delivery");
    handler.handle(&payload).await.expect("duplicate delivery");

    let catalog = committer.catalog();
    assert_eq!(
        catalog
            .list(&CoverageQuery {
                asset_class: Some(lake_contracts::AssetClass::Option),
                data_type: None,
                symbol: None,
                interval: None,
                start: None,
                end: None,
            })
            .expect("coverage")
            .len(),
        2,
        "the duplicate delivery replaced the same catalog keys"
    );
}

#[tokio::test]
async fn queue_handler_rejects_bad_payloads_without_touching_the_committer() {
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let committer = Arc::new(LocalCommitter::new(Arc::new(coverage)));
    let transaction = OptionIngestTransaction::new(
        Arc::new(FakeOptionProvider::new(Vec::new(), Vec::new())),
        Arc::new(StagingPartitionWriter::new()),
        Arc::clone(&committer) as Arc<dyn AtomicIngestCommitter>,
    );
    let handler = OptionQueueHandler::new(Arc::new(transaction), None);
    let payload = json!({
        "asset_class": "option",
        "data_types": ["trade", "quote"],
        "end_time": "2024-01-19T21:00:00Z",
        "expiration_date": "2024-01-19",
        "option_type": "call",
        "session_date": "2024-01-19",
        "start_time": "2024-01-19T14:30:00Z",
        "strike_price": "500",
        "symbol": "SPY240119C00500000",
        "underlying": "SPY",
    });

    let mut bad = payload.clone();
    bad["asset_class"] = json!("equity");
    let error = handler.handle(&bad).await.expect_err("asset class");
    assert_eq!(
        error.to_string(),
        "option queue message must use asset_class 'option'"
    );

    let mut naive = payload.clone();
    naive["start_time"] = json!("2024-01-19T14:30:00");
    let error = handler.handle(&naive).await.expect_err("naive start");
    assert_eq!(
        error.to_string(),
        "option queue field 'start_time' must include a timezone"
    );

    let mut no_types = payload.clone();
    no_types["data_types"] = json!([]);
    let error = handler.handle(&no_types).await.expect_err("empty types");
    assert_eq!(
        error.to_string(),
        "option queue message data_types must be a non-empty array"
    );

    let mut unknown_type = payload.clone();
    unknown_type["data_types"] = json!(["gamma"]);
    let error = handler
        .handle(&unknown_type)
        .await
        .expect_err("unknown type");
    assert_eq!(error.to_string(), "\"gamma\" is not a valid DataType");

    assert_eq!(committer.commit_count(), 0);
}

#[tokio::test]
async fn queue_handler_resolves_the_contract_through_the_persisted_front_set() {
    let coverage = lake_catalog::MemoryCatalogRepository::new();
    let committer = Arc::new(LocalCommitter::new(Arc::new(coverage)));
    let front_sets = Arc::new(lake_catalog::MemoryDailyFrontSetRepository::from_records([
        DailyFrontSet::new("SPY", day(2024, 1, 19), vec![spy_option_contract()]).expect("set"),
    ]));
    let provider = FakeOptionProvider::new(
        vec![option_trade(instant(14, 31, 0, 0), "SPY240119C00500000")],
        Vec::new(),
    );
    let transaction = OptionIngestTransaction::new(
        Arc::new(provider),
        Arc::new(StagingPartitionWriter::new()),
        Arc::clone(&committer) as Arc<dyn AtomicIngestCommitter>,
    );
    let handler = OptionQueueHandler::new(Arc::new(transaction), Some(front_sets));
    let payload = json!({
        "asset_class": "option",
        "data_types": ["trade"],
        "end_time": "2024-01-19T21:00:00Z",
        "expiration_date": "2024-01-19",
        "option_type": "call",
        "session_date": "2024-01-19",
        "start_time": "2024-01-19T14:30:00Z",
        "strike_price": "500",
        "symbol": "SPY240119C00500000",
        "underlying": "SPY",
    });
    handler.handle(&payload).await.expect("handled");

    // A symbol outside the cataloged membership is rejected.
    let mut outside = payload.clone();
    outside["symbol"] = json!("SPY240119P00500000");
    let error = handler.handle(&outside).await.expect_err("not a member");
    assert_eq!(
        error.to_string(),
        "option symbol is not in the cataloged front set"
    );

    // A session date with no persisted front set is rejected.
    let mut other_day = payload.clone();
    other_day["session_date"] = json!("2024-01-22");
    let error = handler.handle(&other_day).await.expect_err("no front set");
    assert_eq!(error.to_string(), "option front set is not cataloged");
}

/// Committer that writes objects and coverage into real local state, shared
/// with the option coverage assertions through its catalog handle.
struct LocalCommitter {
    catalog: Arc<lake_catalog::MemoryCatalogRepository>,
    commits: Mutex<Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)>>,
}

impl LocalCommitter {
    fn new(catalog: Arc<lake_catalog::MemoryCatalogRepository>) -> Self {
        Self {
            catalog,
            commits: Mutex::new(Vec::new()),
        }
    }

    fn catalog(&self) -> Arc<lake_catalog::MemoryCatalogRepository> {
        Arc::clone(&self.catalog)
    }

    fn commit_count(&self) -> usize {
        self.commits.lock().expect("commits").len()
    }
}

impl AtomicIngestCommitter for LocalCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
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
