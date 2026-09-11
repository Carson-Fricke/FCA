use super::*;
use crate::backtest::config::{BacktestConfig, StrategyInstanceConfig};

fn spec() -> SweepSpec {
    let mut base = BacktestConfig::new("bt", 0, 86_400_000_000_000_000, 7).expect("config");
    base.strategy_instances.push(StrategyInstanceConfig {
        instance_id: "momentum".to_string(),
        implementation: "s:M".to_string(),
        bundle_hash: "b".to_string(),
        parameters: BTreeMap::new(),
        symbols: vec!["SPY".to_string()],
        capital_fraction: "0.5".to_string(),
    });
    let level = |text: &str| JsonValue::object([("$decimal", JsonValue::String(text.to_string()))]);
    SweepSpec::grid(
        base,
        "momentum",
        BTreeMap::from([("fast".to_string(), vec![level("5"), level("10")])]),
    )
    .expect("spec")
}

#[test]
fn submit_persists_and_launches_every_point() {
    let mut runner = SweepRunner::new(
        Box::new(RecordingLauncher::default()),
        Box::new(InMemoryRunBlobStore::new()),
        Box::new(InMemoryRunCatalog::new()),
    );
    let spec = spec();
    let submission = runner.submit(&spec).expect("submit");
    assert_eq!(submission.expanded, 2);
    assert_eq!(submission.run_ids.len(), 2);
    assert_eq!(runner.sweep_ids().expect("ids"), vec![spec.sweep_id()]);
}

#[test]
fn status_joins_the_catalog() {
    let spec = spec();
    let mut catalog = InMemoryRunCatalog::new();
    for point in spec.expand() {
        catalog.insert(CatalogRecord {
            run_id: point.run_id,
            status: RunStatus::Complete,
            metrics: BTreeMap::from([("sharpe".to_string(), "1.0".to_string())]),
        });
    }
    let mut runner = SweepRunner::new(
        Box::new(RecordingLauncher::default()),
        Box::new(InMemoryRunBlobStore::new()),
        Box::new(catalog),
    );
    runner.submit(&spec).expect("submit");
    let status = runner.status(&spec.sweep_id()).expect("status");
    assert_eq!(status.complete(), 2);
    assert!(status.is_complete());
}

#[test]
fn record_bytes_are_byte_identical_across_submissions() {
    let spec = spec();
    let mut first = SweepRunner::new(
        Box::new(RecordingLauncher::default()),
        Box::new(InMemoryRunBlobStore::new()),
        Box::new(InMemoryRunCatalog::new()),
    );
    first.submit(&spec).expect("first");
    let mut second = SweepRunner::new(
        Box::new(RecordingLauncher::default()),
        Box::new(InMemoryRunBlobStore::new()),
        Box::new(InMemoryRunCatalog::new()),
    );
    second.submit(&spec).expect("second");
    let key = format!(
        "{}/{RECORD_NAME}",
        sweep_prefix(&spec.sweep_id()).expect("prefix")
    );
    assert_eq!(
        first.blobs.read(&key).expect("first bytes"),
        second.blobs.read(&key).expect("second bytes")
    );
}
