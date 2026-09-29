//! Ensure launcher tests: requested data type only, resume, and rejection.

mod support;

use std::collections::BTreeMap;
use std::sync::Arc;

use lake_catalog::{BarSource, CatalogRepository, CoverageQuery, CoverageStatus, JsonCatalogStore};
use lake_contracts::{AssetClass, DataType};
use lake_coverage::{EnsureJob, EnsureSpec};
use lake_ingestion::{BarsDataProvider, EquityDataProvider};
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
            interval: None,
        },
        gaps: Vec::new(),
    }
}

fn bars_job(symbol: &str, interval: &str) -> EnsureJob {
    EnsureJob {
        job_id: format!("ensure-bars-{symbol}-{interval}"),
        request: EnsureSpec {
            asset_class: AssetClass::Equity,
            data_type: DataType::Bars,
            symbol: symbol.to_string(),
            start_date: day(2024, 1, 19),
            end_date: day(2024, 1, 19),
            interval: Some(interval.to_string()),
        },
        gaps: Vec::new(),
    }
}

fn bars_launcher(
    layout: &LocalLayout,
    environ: &BTreeMap<String, String>,
    provider: &Arc<FakeAlpaca>,
) -> Arc<LocalEnsureLauncher> {
    let equity: Arc<dyn EquityDataProvider> = provider.clone();
    let bars: Arc<dyn BarsDataProvider> = provider.clone();
    Arc::new(
        LocalEnsureLauncher::new(
            layout.clone(),
            environ.clone(),
            load_settings(environ),
            load_universes(environ),
            instant(2024, 1, 22, 0, 0),
        )
        .expect("launcher")
        .with_providers(Some(equity), None)
        .with_bars_provider(Some(bars)),
    )
}

fn listed_bars(layout: &LocalLayout) -> Vec<lake_catalog::CoverageRecord> {
    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    catalog
        .list(&CoverageQuery {
            symbol: Some("GLD".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list")
        .into_iter()
        .filter(|record| record.data_type == DataType::Bars)
        .collect()
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

#[tokio::test]
async fn launcher_ensures_trades_then_provider_bars_and_resumes() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new());
    let launch = bars_launcher(&layout, &environ, &provider).launcher();

    launch(bars_job("GLD", "1min")).await.expect("bars ensure");
    assert_eq!(
        provider.calls(),
        vec![
            ("trade".to_string(), "GLD".to_string()),
            ("bars".to_string(), "GLD".to_string()),
        ],
        "trades are ensured before the bars transaction runs"
    );

    let bars = listed_bars(&layout);
    assert_eq!(bars.len(), 1);
    assert_eq!(bars[0].status, CoverageStatus::Complete);
    assert_eq!(bars[0].interval.as_deref(), Some("1min"));
    assert_eq!(bars[0].bar_source, Some(BarSource::Provider));
    assert_eq!(bars[0].source_revision, bars[0].checksum);
    let path = bars[0].object_path.clone().expect("object path");
    assert!(path.contains("interval=1min"));
    assert!(layout.objects().join(path).is_file());

    // A covered bars window returns immediately: no provider call is repeated.
    launch(bars_job("GLD", "1min")).await.expect("bars resume");
    assert_eq!(provider.calls().len(), 2);
}

#[tokio::test]
async fn launcher_derives_bars_when_the_provider_has_none() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new().empty_bars());
    let launch = bars_launcher(&layout, &environ, &provider).launcher();

    launch(bars_job("GLD", "1min")).await.expect("bars ensure");
    assert_eq!(
        provider.calls(),
        vec![
            ("trade".to_string(), "GLD".to_string()),
            ("bars".to_string(), "GLD".to_string()),
        ]
    );

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let records = catalog
        .list(&CoverageQuery {
            symbol: Some("GLD".to_string()),
            ..CoverageQuery::default()
        })
        .expect("list");
    let trade = records
        .iter()
        .find(|record| record.data_type == DataType::Trade)
        .expect("trade record");
    let bars = records
        .iter()
        .find(|record| record.data_type == DataType::Bars)
        .expect("bars record");
    assert_eq!(bars.status, CoverageStatus::Complete);
    assert_eq!(bars.row_count, 1);
    assert_eq!(bars.bar_source, Some(BarSource::Derived));
    assert_eq!(
        bars.source_revision, trade.checksum,
        "derived bars record the trade partition revision"
    );
}

#[tokio::test]
async fn launcher_derives_bars_when_the_provider_range_is_unsupported() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temporary.path().join("local"));
    let environ = credentials();
    let provider = Arc::new(FakeAlpaca::new().unsupported_bars());
    let launch = bars_launcher(&layout, &environ, &provider).launcher();

    launch(bars_job("GLD", "1d")).await.expect("bars ensure");
    let bars = listed_bars(&layout);
    assert_eq!(bars.len(), 1);
    assert_eq!(bars[0].status, CoverageStatus::Complete);
    assert_eq!(bars[0].bar_source, Some(BarSource::Derived));
    assert!(bars[0].error_message.is_none());
}

#[tokio::test]
async fn launcher_rejects_bars_without_an_interval() {
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
    let mut bars = bars_job("AAPL", "1min");
    bars.request.interval = None;
    let error = launcher.launcher()(bars)
        .await
        .expect_err("bars without interval");
    assert!(error.to_string().contains("bars requires an interval"));
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
