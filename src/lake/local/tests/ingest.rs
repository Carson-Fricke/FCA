//! End-to-end local ingest tests over a fake provider.

mod support;

use std::collections::BTreeMap;
use std::sync::Arc;

use lake_catalog::{CatalogRepository, CoverageQuery, CoverageStatus, JsonCatalogStore};
use lake_contracts::{AssetClass, DataType};
use lake_ingestion::EquityDataProvider;
use lake_local::{
    run_local_ingest, IngestCoverageEntry, LocalIngestError, LocalIngestOptions, LocalLayout,
};
use serde_json::Value;

use support::{credentials, day, instant, load_settings, load_universes, FakeAlpaca};

fn options(
    layout: &LocalLayout,
    symbol: Option<&str>,
    environ: &BTreeMap<String, String>,
    provider: Arc<FakeAlpaca>,
) -> LocalIngestOptions {
    let injected: Arc<dyn EquityDataProvider> = provider;
    LocalIngestOptions {
        layout: layout.clone(),
        symbol: symbol.map(str::to_string),
        session_date: Some(day(2024, 1, 19)),
        environ: environ.clone(),
        now: instant(2024, 1, 22, 0, 0),
        universes: load_universes(environ),
        settings: load_settings(environ),
        provider: Some(injected),
        catalog: None,
    }
}

#[tokio::test]
async fn fake_provider_ingest_persists_parquet_and_catalog() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new());

    let result = run_local_ingest(options(
        &layout,
        Some("aapl"),
        &environ,
        Arc::clone(&provider),
    ))
    .await
    .expect("ingest");

    assert_eq!(
        provider.calls(),
        vec![
            ("trade".to_string(), "AAPL".to_string()),
            ("quote".to_string(), "AAPL".to_string()),
        ]
    );
    assert_eq!(result.symbol, "AAPL");
    assert_eq!(result.session_date, "2024-01-19");
    assert_eq!(
        result.coverage,
        vec![
            IngestCoverageEntry {
                data_type: "trade".to_string(),
                rows: 1,
                status: "complete".to_string(),
            },
            IngestCoverageEntry {
                data_type: "quote".to_string(),
                rows: 1,
                status: "complete".to_string(),
            },
        ]
    );
    assert_eq!(result.catalog, layout.catalog_path().to_string_lossy());
    assert_eq!(result.data_root, layout.root().to_string_lossy());
    assert_eq!(result.objects.len(), 2);
    for object in &result.objects {
        assert_eq!(object.rows, 1);
        assert!(object.bytes > 0);
        assert!(layout.objects().join(&object.path).is_file());
    }

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            asset_class: Some(AssetClass::Equity),
            symbol: Some("AAPL".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(records.len(), 2);
    assert_eq!(
        records
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Complete, CoverageStatus::Complete]
    );
    assert_eq!(
        records
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Quote, DataType::Trade]
    );
    for record in &records {
        let path = layout
            .objects()
            .join(record.object_path.clone().expect("object path"));
        assert!(path.is_file());
    }

    let text = std::fs::read_to_string(layout.catalog_path()).expect("catalog text");
    assert!(text.ends_with("],\"schema_version\":1}\n"));
    let payload: Value = serde_json::from_str(&text).expect("catalog json");
    assert_eq!(payload["schema_version"], 1);
    assert_eq!(payload["coverage"].as_array().expect("rows").len(), 2);

    let serialized = serde_json::to_value(&result).expect("result json");
    let keys: Vec<&String> = serialized
        .as_object()
        .expect("result object")
        .keys()
        .collect();
    assert_eq!(
        keys,
        vec![
            "catalog",
            "coverage",
            "data_root",
            "objects",
            "session_date",
            "symbol"
        ]
    );
}

#[tokio::test]
async fn missing_credentials_fail_before_provider_or_state_use() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("must-not-exist"));
    let environ = BTreeMap::new();
    let provider = Arc::new(FakeAlpaca::new());

    let error = run_local_ingest(options(&layout, None, &environ, Arc::clone(&provider)))
        .await
        .expect_err("missing credentials");

    assert!(error.message.contains("API_KEY"));
    assert!(error.message.contains("API_SECRET"));
    assert!(provider.calls().is_empty());
    assert!(!layout.root().exists());
}

#[tokio::test]
async fn ingest_accepts_symbols_outside_the_configured_universe() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new());

    let result = run_local_ingest(options(
        &layout,
        Some("MSFT"),
        &environ,
        Arc::clone(&provider),
    ))
    .await
    .expect("ingest");

    assert_eq!(
        provider.calls(),
        vec![
            ("trade".to_string(), "MSFT".to_string()),
            ("quote".to_string(), "MSFT".to_string()),
        ]
    );
    assert_eq!(result.symbol, "MSFT");
}

#[tokio::test]
async fn provider_failure_is_persisted_but_returns_a_clear_error() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new().poisoning(day(2024, 1, 19)));

    let error: LocalIngestError =
        run_local_ingest(options(&layout, None, &environ, Arc::clone(&provider)))
            .await
            .expect_err("poisoned ingest");

    assert!(error.message.contains("poisoned session"));
    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            asset_class: Some(AssetClass::Equity),
            symbol: Some("AAPL".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(records.len(), 2);
    assert!(records
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert!(records.iter().all(|record| record
        .error_message
        .as_deref()
        .unwrap_or_default()
        .contains("poisoned session")));
}
