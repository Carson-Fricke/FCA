//! Ensure launcher tests: requested data type only, resume, and rejection.

mod support;

use std::collections::BTreeMap;
use std::sync::Arc;

use lake_catalog::{CatalogRepository, CoverageQuery, CoverageStatus, JsonCatalogStore};
use lake_contracts::{AssetClass, DataType};
use lake_coverage::{EnsureJob, EnsureSpec};
use lake_ingestion::EquityDataProvider;
use lake_local::{
    local_ensure_deadline_seconds, LocalEnsureLauncher, LocalLayout,
    DEFAULT_ENSURE_DEADLINE_SECONDS,
};

use support::{credentials, day, instant, load_settings, load_universes, FakeAlpaca};

fn job(asset_class: AssetClass, data_type: DataType, symbol: &str) -> EnsureJob {
    EnsureJob {
        job_id: format!("ensure-{}-{}", data_type.value(), symbol),
        request: EnsureSpec {
            asset_class,
            data_type,
            symbol: symbol.to_string(),
            start_date: day(2024, 1, 19),
            end_date: day(2024, 1, 19),
        },
        gaps: Vec::new(),
    }
}

#[tokio::test]
async fn launcher_fetches_only_the_requested_data_type_and_resumes() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new());
    let injected: Arc<dyn EquityDataProvider> = provider.clone();
    let launcher = Arc::new(
        LocalEnsureLauncher::new(
            layout.clone(),
            environ.clone(),
            load_settings(&environ),
            load_universes(&environ),
            instant(2024, 1, 22, 0, 0),
        )
        .expect("launcher")
        .with_providers(Some(injected), None),
    );
    let launch = launcher.launcher();

    launch(job(AssetClass::Equity, DataType::Trade, "GLD"))
        .await
        .expect("trade ensure");
    assert_eq!(
        provider.calls(),
        vec![("trade".to_string(), "GLD".to_string())]
    );
    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            symbol: Some("GLD".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(
        records
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Trade]
    );
    assert!(records
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));
    let object_path = records[0].object_path.clone().expect("object path");
    assert!(layout.objects().join(object_path).is_file());

    // Terminality is judged against the requested datasets only.
    launch(job(AssetClass::Equity, DataType::Trade, "GLD"))
        .await
        .expect("trade resume");
    assert_eq!(provider.calls().len(), 1);

    // The quote ensure then fills the other half without redoing trades.
    launch(job(AssetClass::Equity, DataType::Quote, "GLD"))
        .await
        .expect("quote ensure");
    assert_eq!(
        provider.calls(),
        vec![
            ("trade".to_string(), "GLD".to_string()),
            ("quote".to_string(), "GLD".to_string()),
        ]
    );
    let refreshed = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = refreshed
        .list(&CoverageQuery {
            symbol: Some("GLD".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    assert_eq!(
        records
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Quote, DataType::Trade]
    );
    assert!(records
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));
}

#[tokio::test]
async fn launcher_rejects_option_requests() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let launcher = Arc::new(
        LocalEnsureLauncher::new(
            layout,
            environ.clone(),
            load_settings(&environ),
            load_universes(&environ),
            instant(2024, 1, 22, 0, 0),
        )
        .expect("launcher"),
    );
    let error = launcher.launcher()(job(AssetClass::Option, DataType::Trade, "SPY"))
        .await
        .expect_err("option rejection");
    assert!(error
        .to_string()
        .contains("options ingest is not supported by the local launcher"));
}

#[test]
fn ensure_deadline_reads_the_environment_with_a_positive_bound() {
    assert_eq!(
        local_ensure_deadline_seconds(&BTreeMap::new()).expect("default"),
        DEFAULT_ENSURE_DEADLINE_SECONDS
    );
    let blank = BTreeMap::from([(
        "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS".to_string(),
        "   ".to_string(),
    )]);
    assert_eq!(
        local_ensure_deadline_seconds(&blank).expect("blank default"),
        DEFAULT_ENSURE_DEADLINE_SECONDS
    );
    let configured = BTreeMap::from([(
        "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS".to_string(),
        "2.5".to_string(),
    )]);
    assert_eq!(
        local_ensure_deadline_seconds(&configured).expect("configured"),
        2.5
    );
    let zero = BTreeMap::from([(
        "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS".to_string(),
        "0".to_string(),
    )]);
    let error = local_ensure_deadline_seconds(&zero).expect_err("zero");
    assert!(error.message.contains("must be positive"));
    let invalid = BTreeMap::from([(
        "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS".to_string(),
        "soon".to_string(),
    )]);
    let error = local_ensure_deadline_seconds(&invalid).expect_err("invalid");
    assert!(error.message.contains("must be a number"));
}
