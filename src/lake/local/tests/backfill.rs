//! Backfill walk tests: resume, skip, poisoning, transient retry, and shapes.

mod support;

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use lake_catalog::{CatalogRepository, CoverageQuery, CoverageStatus, JsonCatalogStore};
use lake_contracts::{AssetClass, DataType};
use lake_ingestion::{CryptoDataProvider, EquityDataProvider};
use lake_local::{
    run_local_backfill, run_local_crypto_backfill, CryptoBackfillResult, LocalBackfillOptions,
    LocalCryptoBackfillOptions, LocalLayout, SessionIngestEntry,
};

use support::{credentials, day, instant, load_settings, FakeAlpaca, FakeCrypto};

fn equity_options(
    layout: &LocalLayout,
    start: lake_contracts::Date,
    end: lake_contracts::Date,
    provider: Arc<FakeAlpaca>,
) -> LocalBackfillOptions {
    let environ = credentials();
    let injected: Arc<dyn EquityDataProvider> = provider;
    LocalBackfillOptions {
        layout: layout.clone(),
        symbols: vec!["msft".to_string()],
        start_date: start,
        end_date: end,
        data_types: None,
        environ: environ.clone(),
        now: instant(2024, 1, 22, 0, 0),
        settings: load_settings(&environ),
        provider: Some(injected),
        retry_settle_seconds: Some(0.0),
        catalog: None,
    }
}

fn two_sessions(layout: &LocalLayout, provider: Arc<FakeAlpaca>) -> LocalBackfillOptions {
    equity_options(layout, day(2024, 1, 18), day(2024, 1, 19), provider)
}

#[tokio::test]
async fn backfill_fetches_an_arbitrary_symbol_once_and_resumes() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(FakeAlpaca::new());

    let first = run_local_backfill(equity_options(
        &layout,
        day(2024, 1, 19),
        day(2024, 1, 19),
        Arc::clone(&provider),
    ))
    .await
    .expect("first backfill");

    assert_eq!(
        provider.calls(),
        vec![
            ("trade".to_string(), "MSFT".to_string()),
            ("quote".to_string(), "MSFT".to_string()),
        ]
    );
    assert_eq!(first.symbols, vec!["MSFT".to_string()]);
    assert_eq!(first.requested_start, "2024-01-19");
    assert_eq!(first.requested_end, "2024-01-19");
    assert!(first.errors.is_empty());
    assert_eq!(
        first.sessions,
        vec![SessionIngestEntry {
            data_type_rows: BTreeMap::from([("quote".to_string(), 1), ("trade".to_string(), 1),]),
            error: None,
            retryable: false,
            session_date: "2024-01-19".to_string(),
            statuses: vec!["complete".to_string(), "complete".to_string()],
            symbol: "MSFT".to_string(),
        }]
    );
    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            symbol: Some("MSFT".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert!(records
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));

    // A rerun reads the catalog checkpoint and does not refetch.
    let second = run_local_backfill(equity_options(
        &layout,
        day(2024, 1, 19),
        day(2024, 1, 19),
        Arc::clone(&provider),
    ))
    .await
    .expect("second backfill");
    assert!(second.sessions.is_empty());
    assert_eq!(provider.calls().len(), 2);

    let serialized = serde_json::to_value(&first).expect("result json");
    let keys: Vec<&String> = serialized
        .as_object()
        .expect("result object")
        .keys()
        .collect();
    assert_eq!(
        keys,
        vec![
            "data_root",
            "errors",
            "requested_end",
            "requested_start",
            "sessions",
            "symbols"
        ]
    );
}

#[tokio::test]
async fn backfill_survives_a_poisoned_session_and_surfaces_the_error() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(FakeAlpaca::new().poisoning(day(2024, 1, 19)));

    let result = run_local_backfill(two_sessions(&layout, Arc::clone(&provider)))
        .await
        .expect("backfill");

    assert_eq!(
        result
            .sessions
            .iter()
            .map(|entry| entry.session_date.as_str())
            .collect::<Vec<_>>(),
        vec!["2024-01-18", "2024-01-19"]
    );
    let healthy = &result.sessions[0];
    let poisoned = &result.sessions[1];
    assert_eq!(
        healthy.statuses,
        vec!["complete".to_string(), "complete".to_string()]
    );
    assert_eq!(healthy.error, None);
    assert_eq!(
        poisoned.statuses,
        vec!["error".to_string(), "error".to_string()]
    );
    assert_eq!(
        poisoned.error.as_deref(),
        Some("trade: ValueError: poisoned session")
    );
    assert!(!poisoned.retryable);
    assert_eq!(result.errors, vec![poisoned.clone()]);

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            symbol: Some("MSFT".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(
        records
            .iter()
            .map(|record| (record.session_date.isoformat(), record.status))
            .collect::<Vec<_>>(),
        vec![
            ("2024-01-18".to_string(), CoverageStatus::Complete),
            ("2024-01-19".to_string(), CoverageStatus::Error),
            ("2024-01-18".to_string(), CoverageStatus::Complete),
            ("2024-01-19".to_string(), CoverageStatus::Error),
        ]
    );
    assert!(records
        .iter()
        .filter(|record| record.session_date.isoformat() == "2024-01-19")
        .all(|record| record
            .error_message
            .as_deref()
            .unwrap_or_default()
            .contains("poisoned session")));

    // Error coverage stays non-terminal: a rerun retries only that session.
    let healed = run_local_backfill(two_sessions(&layout, Arc::new(FakeAlpaca::new())))
        .await
        .expect("healed backfill");
    assert_eq!(
        healed
            .sessions
            .iter()
            .map(|entry| entry.session_date.as_str())
            .collect::<Vec<_>>(),
        vec!["2024-01-19"]
    );
    assert_eq!(
        healed.sessions[0].statuses,
        vec!["complete".to_string(), "complete".to_string()]
    );
    let healed_catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    assert!(healed_catalog
        .list(&CoverageQuery {
            symbol: Some("MSFT".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list")
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));
}

#[tokio::test]
async fn backfill_raises_only_when_nothing_could_be_processed() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(
        FakeAlpaca::new()
            .poisoning(day(2024, 1, 18))
            .poisoning(day(2024, 1, 19)),
    );

    let error = run_local_backfill(two_sessions(&layout, provider))
        .await
        .expect_err("nothing processed");
    assert!(error.message.contains("poisoned session"));

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            symbol: Some("MSFT".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(records.len(), 4);
    assert!(records
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
}

#[tokio::test]
async fn backfill_retries_transiently_failed_sessions_in_run() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(FakeAlpaca::new().blipping(day(2024, 1, 19), 3));

    let result = run_local_backfill(two_sessions(&layout, Arc::clone(&provider)))
        .await
        .expect("transient retry backfill");

    assert!(result.errors.is_empty());
    assert_eq!(
        result
            .sessions
            .iter()
            .map(|entry| entry.statuses.clone())
            .collect::<Vec<_>>(),
        vec![
            vec!["complete".to_string(), "complete".to_string()],
            vec!["complete".to_string(), "complete".to_string()],
        ]
    );
    assert!(result
        .sessions
        .iter()
        .all(|entry| !entry.retryable && entry.error.is_none()));
    // Three in-transaction attempts plus the one in-run retry pass.
    assert_eq!(provider.trade_calls(day(2024, 1, 19)), 4);
    assert_eq!(provider.trade_calls(day(2024, 1, 18)), 1);

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    assert!(catalog
        .list(&CoverageQuery {
            symbol: Some("MSFT".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list")
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));
}

#[tokio::test]
async fn backfill_does_not_retry_permanent_session_failures() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(FakeAlpaca::new().poisoning(day(2024, 1, 19)));

    let result = run_local_backfill(two_sessions(&layout, Arc::clone(&provider)))
        .await
        .expect("permanent failure backfill");

    let poisoned = &result.sessions[1];
    assert_eq!(
        poisoned.statuses,
        vec!["error".to_string(), "error".to_string()]
    );
    assert_eq!(
        poisoned.error.as_deref(),
        Some("trade: ValueError: poisoned session")
    );
    assert!(!poisoned.retryable);
    assert_eq!(result.errors, vec![poisoned.clone()]);
    assert_eq!(provider.trade_calls(day(2024, 1, 19)), 1);
}

#[tokio::test]
async fn backfill_runs_sessions_concurrently() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(FakeAlpaca::new().slow(Duration::from_millis(50)));

    let result = run_local_backfill(two_sessions(&layout, Arc::clone(&provider)))
        .await
        .expect("backfill");

    assert_eq!(result.sessions.len(), 2);
    assert!(provider.max_in_flight() > 1);
}

#[tokio::test]
async fn crypto_backfill_survives_a_poisoned_day_and_returns_the_crypto_shape() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let provider = Arc::new(FakeCrypto::new().poisoning(day(2024, 1, 2)));
    let environ = credentials();
    let injected: Arc<dyn CryptoDataProvider> = provider;

    let result: CryptoBackfillResult = run_local_crypto_backfill(LocalCryptoBackfillOptions {
        layout: layout.clone(),
        pairs: vec!["btc/usd".to_string()],
        start_date: day(2024, 1, 1),
        end_date: day(2024, 1, 2),
        data_types: None,
        environ: environ.clone(),
        now: instant(2024, 1, 3, 12, 0),
        settings: load_settings(&environ),
        provider: Some(injected),
        retry_settle_seconds: Some(0.0),
        catalog: None,
    })
    .await
    .expect("crypto backfill");

    assert_eq!(result.asset_class, "crypto");
    assert_eq!(result.symbols, vec!["BTC/USD".to_string()]);
    let healthy = &result.sessions[0];
    let poisoned = &result.sessions[1];
    assert_eq!(
        healthy.statuses,
        vec!["complete".to_string(), "complete".to_string()]
    );
    assert_eq!(healthy.error, None);
    assert_eq!(
        poisoned.statuses,
        vec!["error".to_string(), "error".to_string()]
    );
    assert_eq!(
        poisoned.error.as_deref(),
        Some("trade: ValueError: poisoned day")
    );
    assert_eq!(result.errors, vec![poisoned.clone()]);

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            asset_class: Some(AssetClass::Crypto),
            symbol: Some("BTC/USD".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(
        records
            .iter()
            .map(|record| (record.session_date.isoformat(), record.status))
            .collect::<Vec<_>>(),
        vec![
            ("2024-01-01".to_string(), CoverageStatus::Complete),
            ("2024-01-02".to_string(), CoverageStatus::Error),
            ("2024-01-01".to_string(), CoverageStatus::Complete),
            ("2024-01-02".to_string(), CoverageStatus::Error),
        ]
    );
    assert_eq!(
        records
            .iter()
            .filter(|record| record.data_type == DataType::Trade)
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Complete, CoverageStatus::Error]
    );

    let serialized = serde_json::to_value(&result).expect("result json");
    let keys: Vec<&String> = serialized
        .as_object()
        .expect("result object")
        .keys()
        .collect();
    assert_eq!(
        keys,
        vec![
            "asset_class",
            "data_root",
            "errors",
            "requested_end",
            "requested_start",
            "sessions",
            "symbols"
        ]
    );
}
