//! A bars ensure fetches bars only: it must never call the trade or quote
//! provider even when the window is cold.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use futures_util::future::BoxFuture;
use lake_catalog::{BarSource, CatalogRepository, JsonCatalogStore};
use lake_configuration::{LakeSettings, UniverseSettings};
use lake_contracts::{AssetClass, BarInterval, DataType, Date, DateTime, Decimal18};
use lake_coverage::{EnsureJob, EnsureSpec};
use lake_ingestion::{BarsDataProvider, EquityDataProvider};
use lake_local::{LocalEnsureLauncher, LocalLayout};
use lake_providers::{NormalizedBar, NormalizedQuote, NormalizedTrade, ProviderError};

fn instant(year: i32, month: u32, day: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day, hour, minute, 0, 0).expect("instant")
}

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).expect("date")
}

fn credentials() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("LAKE_PROVIDER__API_KEY".to_string(), "test-key".to_string()),
        (
            "LAKE_PROVIDER__API_SECRET".to_string(),
            "test-secret".to_string(),
        ),
    ])
}

fn collect_files(root: &Path, files: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_files(&path, files);
        } else {
            files.push(path);
        }
    }
}

#[derive(Default)]
struct RecordingEquityProvider {
    calls: AtomicUsize,
}

impl RecordingEquityProvider {
    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl EquityDataProvider for RecordingEquityProvider {
    fn fetch_trades<'a>(
        &'a self,
        _symbols: &'a [String],
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move { Ok(Vec::new()) })
    }

    fn fetch_quotes<'a>(
        &'a self,
        _symbols: &'a [String],
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move { Ok(Vec::new()) })
    }
}

struct FixedBarsProvider {
    bars: Vec<NormalizedBar>,
}

impl BarsDataProvider for FixedBarsProvider {
    fn fetch_bars<'a>(
        &'a self,
        _asset_class: AssetClass,
        _symbol: &'a str,
        _interval: BarInterval,
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        Box::pin(async move { Ok(self.bars.clone()) })
    }
}

#[tokio::test]
async fn bars_ensure_fetches_bars_only_and_never_touches_the_equity_provider() {
    let environ = credentials();
    let settings = LakeSettings::load(None, &environ).expect("settings");
    let universes = UniverseSettings::load(None, &environ).expect("universes");
    let temp = tempfile::tempdir().expect("tempdir");
    let layout = LocalLayout::new(temp.path().join("market-lake"));
    let now = instant(2024, 1, 20, 1, 0);

    let equity = Arc::new(RecordingEquityProvider::default());
    let bars = Arc::new(FixedBarsProvider {
        bars: vec![NormalizedBar {
            timestamp: instant(2024, 1, 18, 15, 0),
            symbol: "AAPL".to_string(),
            open: Decimal18::parse("185.00").expect("open"),
            high: Decimal18::parse("186.00").expect("high"),
            low: Decimal18::parse("184.50").expect("low"),
            close: Decimal18::parse("185.50").expect("close"),
            volume: Decimal18::parse("1000").expect("volume"),
        }],
    });

    let launcher = Arc::new(
        LocalEnsureLauncher::new(layout.clone(), environ, settings, universes, now)
            .expect("launcher")
            .with_providers(
                Some(Arc::clone(&equity) as Arc<dyn EquityDataProvider>),
                None,
            )
            .with_bars_provider(Some(Arc::clone(&bars) as Arc<dyn BarsDataProvider>)),
    );
    let launch = launcher.launcher();

    launch(EnsureJob {
        job_id: "job-bars-only".to_string(),
        request: EnsureSpec {
            asset_class: AssetClass::Equity,
            data_type: DataType::Bars,
            symbol: "AAPL".to_string(),
            start_date: date(2024, 1, 18),
            end_date: date(2024, 1, 18),
            interval: Some("1min".to_string()),
        },
        gaps: Vec::new(),
    })
    .await
    .expect("bars ensure succeeds");

    assert_eq!(
        equity.calls(),
        0,
        "a bars ensure must never call the equity trade or quote provider"
    );

    let mut files: Vec<PathBuf> = Vec::new();
    collect_files(&layout.objects(), &mut files);
    assert!(
        files
            .iter()
            .any(|path| path.to_string_lossy().contains("data_type=bars")),
        "the provider bars object was not written"
    );
    for path in &files {
        let rendered = path.to_string_lossy();
        assert!(
            !rendered.contains("data_type=trade"),
            "a bars ensure wrote a trade object: {rendered}"
        );
        assert!(
            !rendered.contains("data_type=quote"),
            "a bars ensure wrote a quote object: {rendered}"
        );
    }

    let catalog = JsonCatalogStore::open(layout.catalog_path()).expect("catalog");
    let record = catalog
        .get(
            AssetClass::Equity,
            DataType::Bars,
            Some("1min"),
            "AAPL",
            date(2024, 1, 18),
        )
        .expect("catalog get")
        .expect("bars coverage row");
    assert_eq!(record.bar_source, Some(BarSource::Provider));
}
