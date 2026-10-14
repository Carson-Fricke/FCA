//! Coverage planning must remain bounded by the requested Azure catalog partitions.
use std::sync::{Arc, Mutex};

use lake_catalog::{AzureTableCatalogRepository, TableEntity, TableEntityStore, TableQuery};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime};
use lake_coverage::calculate_coverage;

#[derive(Default)]
struct Table {
    queries: Mutex<Vec<TableQuery>>,
}

impl TableEntityStore for Table {
    fn get_entity(&self, _: &str, _: &str) -> Result<TableEntity, DataError> {
        Err(DataError::NotFound("absent".into()))
    }
    fn upsert_entity(&self, _: TableEntity) -> Result<(), DataError> {
        panic!("coverage planning cannot mutate storage")
    }
    fn query_entities(&self, query: &TableQuery) -> Result<Vec<TableEntity>, DataError> {
        self.queries.lock().unwrap().push(query.clone());
        Ok(Vec::new())
    }
    fn delete_entity(&self, _: &str, _: &str) -> Result<(), DataError> {
        panic!("coverage planning cannot mutate storage")
    }
}

#[test]
fn coverage_reads_only_selected_partitions_and_bars_source_revisions() {
    for (data_type, interval, expected_datasets) in [
        (DataType::Quote, None, vec!["quote"]),
        (DataType::Bars, Some("1min"), vec!["bars", "trade"]),
    ] {
        let table = Arc::new(Table::default());
        let catalog = AzureTableCatalogRepository::new(table.clone());
        let date = Date::parse("2024-01-18").unwrap();
        let as_of = DateTime::parse_iso_utc("2024-01-20T00:00:00Z").unwrap();
        let report = calculate_coverage(
            &catalog,
            None,
            AssetClass::Equity,
            &[data_type],
            &["MSFT".into(), "SPY".into()],
            interval,
            date,
            date,
            as_of,
        )
        .unwrap();
        assert_eq!(report.gap_plan().gaps.len(), 2);
        let queries = table.queries.lock().unwrap();
        assert_eq!(queries.len(), 2 * expected_datasets.len());
        for query in queries.iter() {
            assert_eq!(query.filter, "PartitionKey eq @partition");
        }
        for symbol in ["MSFT", "SPY"] {
            for dataset in &expected_datasets {
                let expected = format!(
                    "coverage|equity|{dataset}|{symbol}{}",
                    if *dataset == "bars" {
                        "|interval=1min"
                    } else {
                        ""
                    }
                );
                assert!(
                    queries
                        .iter()
                        .any(|q| q.parameters["partition"] == expected),
                    "{expected}"
                );
            }
        }
    }
}
