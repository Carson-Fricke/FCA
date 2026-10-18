//! Coverage discovery reads only the requested symbol's committed manifests.
use lake_catalog::PublicationCatalog;
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime};
use lake_coverage::calculate_coverage;
use lake_storage::{BlobStore, Lease, LeaseProvider, ReadableBlobStore, SymbolDayScope};
use std::sync::{Arc, Mutex};
#[derive(Default)]
struct Objects(Mutex<Vec<String>>);
impl BlobStore for Objects {
    fn replace_file(&self, path: &str, file: &std::path::Path) -> Result<(), DataError> {
        self.replace(path, &std::fs::read(file)?)
    }
    fn replace(&self, _: &str, _: &[u8]) -> Result<(), DataError> {
        panic!("read-only planning")
    }
}
impl ReadableBlobStore for Objects {
    fn read(&self, _: &str) -> Result<Vec<u8>, DataError> {
        panic!("no manifests listed")
    }
    fn list_prefix(&self, prefix: &str) -> Result<Vec<String>, DataError> {
        self.0.lock().unwrap().push(prefix.into());
        Ok(Vec::new())
    }
    fn delete_prefix(&self, _: &str) -> Result<u64, DataError> {
        panic!("read-only planning")
    }
}
struct Locks;
impl LeaseProvider for Locks {
    fn acquire(&self, _: &SymbolDayScope) -> Result<Box<dyn Lease>, DataError> {
        panic!("read-only planning")
    }
}
#[test]
fn coverage_discovery_uses_selected_symbols_without_mutation() {
    for (data, interval, per_symbol) in [
        (DataType::Quote, None, 1),
        (DataType::Bars, Some("1min"), 2),
    ] {
        let objects = Arc::new(Objects::default());
        let catalog = PublicationCatalog::new(objects.clone(), Arc::new(Locks));
        let date = Date::parse("2024-01-18").unwrap();
        let report = calculate_coverage(
            &catalog,
            None,
            AssetClass::Equity,
            &[data],
            &["MSFT".into(), "SPY".into()],
            interval,
            date,
            date,
            DateTime::parse_iso_utc("2024-01-20T00:00:00Z").unwrap(),
        )
        .unwrap();
        assert_eq!(report.gap_plan().gaps.len(), 2);
        let queries = objects.0.lock().unwrap();
        assert_eq!(queries.len(), 2 * per_symbol);
        for symbol in ["MSFT", "SPY"] {
            assert_eq!(
                queries
                    .iter()
                    .filter(|p| **p == format!("publications/asset_class=equity/symbol={symbol}"))
                    .count(),
                per_symbol
            );
        }
    }
}
