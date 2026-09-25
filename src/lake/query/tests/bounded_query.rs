//! Bounded DuckDB engine behavior over ensured local Parquet fixtures.
//!
//! Ports the executor-level cases of `tests/lake/query/test_bounded_query.py`:
//! only canonical ensured files become scan targets, force is fail-closed,
//! byte limits gate the conservative scan estimate and both output formats,
//! and decimal128 columns render exactly like the Python
//! `to_jsonable_python` path (`str(Decimal)`).

use std::sync::Arc;

use duckdb::arrow::array::AsArray;
use duckdb::arrow::datatypes::UInt64Type;
use duckdb::arrow::ipc::reader::StreamReader;
use lake_contracts::{partition_path, AssetClass, DataType, Date};
use lake_query::{
    validate_read_only_sql, BoundedDuckDbQuery, BoundedQueryOutput, EnsuredQueryFile, QueryLimits,
};
use lake_storage::{LocalFilesystemStore, ParquetWriter};
use serde_json::json;
use tempfile::TempDir;

fn session_date(day: u32) -> Date {
    Date::from_ymd(2024, 1, day).expect("valid date")
}

fn canonical_path(day: u32) -> String {
    partition_path(
        session_date(day),
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
    )
    .expect("canonical path")
}

fn trade_record(trade_id: i64) -> serde_json::Value {
    json!({
        "timestamp": "2024-01-18T14:30:00Z",
        "symbol": "AAPL",
        "trade_id": trade_id,
        "exchange": "XNAS",
        "price": "190.125",
        "size": 100,
        "conditions": ["@"],
        "tape": "C",
        "update": null,
    })
}

/// Write one AAPL trade fixture for its canonical partition.
fn write_fixture(root: &TempDir, day: u32, trade_ids: &[i64]) -> (String, u64) {
    let store = Arc::new(LocalFilesystemStore::new(root.path()));
    let records: Vec<serde_json::Value> = trade_ids
        .iter()
        .map(|trade_id| trade_record(*trade_id))
        .collect();
    let metadata = ParquetWriter::new(store)
        .write(
            &records,
            session_date(day),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        )
        .expect("fixture written");
    (metadata.path, metadata.bytes)
}

/// The on-disk byte size of one written fixture partition.
fn fixture_size(root: &TempDir, day: u32) -> u64 {
    let mut local = root.path().to_path_buf();
    for part in canonical_path(day).split('/') {
        local.push(part);
    }
    std::fs::metadata(&local).expect("fixture metadata").len()
}

fn ensured_file(day: u32, byte_size: u64) -> EnsuredQueryFile {
    EnsuredQueryFile::new(
        AssetClass::Equity,
        DataType::Trade,
        "aapl",
        session_date(day),
        canonical_path(day),
        byte_size,
    )
}

fn executor(root: &TempDir, max_scan_bytes: u64, max_result_bytes: u64) -> BoundedDuckDbQuery {
    BoundedDuckDbQuery::new(
        root.path(),
        QueryLimits::new(max_scan_bytes, max_result_bytes).expect("valid limits"),
    )
}

fn select_ordered() -> &'static str {
    "SELECT trade_id, price FROM data ORDER BY trade_id"
}

fn select_star() -> &'static str {
    "SELECT * FROM data"
}

#[test]
fn execute_reads_the_ensured_fixture_and_renders_json_metadata() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1, 2]);
    let file = ensured_file(18, fixture_size(&root, 18));
    let query = executor(&root, 1_073_741_824, 33_554_432);

    let result = query
        .execute(
            select_ordered(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect("bounded result");

    assert_eq!(
        result.columns,
        vec!["trade_id".to_string(), "price".to_string()]
    );
    assert_eq!(result.rows.len(), 2);
    assert_eq!(result.rows[0]["trade_id"], json!(1));
    assert_eq!(result.rows[0]["price"], json!("190.125000000000000000"));
    assert_eq!(result.rows[1]["trade_id"], json!(2));
    // The fixture writer pins one price for every trade row.
    assert_eq!(result.rows[1]["price"], json!("190.125000000000000000"));
    assert!(result.estimated_scan_bytes > 0);
    assert!(result.result_bytes > 0);
    assert!(!result.forced);
    assert!(result.arrow_bytes.is_empty());
}

#[test]
fn execute_can_return_an_arrow_ipc_stream() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1, 2]);
    let file = ensured_file(18, fixture_size(&root, 18));
    let query = executor(&root, 1_073_741_824, 33_554_432);

    let result = query
        .execute(
            select_ordered(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Arrow,
        )
        .expect("bounded arrow result");

    assert!(result.rows.is_empty());
    assert_eq!(result.result_bytes, result.arrow_bytes.len() as u64);
    let reader = StreamReader::try_new(result.arrow_bytes.as_slice(), None).expect("arrow stream");
    let schema = reader.schema().clone();
    let names: Vec<String> = schema
        .fields()
        .iter()
        .map(|field| field.name().clone())
        .collect();
    assert_eq!(names, vec!["trade_id".to_string(), "price".to_string()]);
    let mut rows = 0usize;
    let mut trade_ids = Vec::new();
    for batch in reader {
        let batch = batch.expect("record batch");
        rows += batch.num_rows();
        let column = batch.column(0).as_primitive::<UInt64Type>();
        for index in 0..batch.num_rows() {
            trade_ids.push(column.value(index));
        }
    }
    assert_eq!(rows, 2);
    assert_eq!(trade_ids, vec![1u64, 2u64]);
}

#[test]
fn executor_rejects_noncanonical_paths_before_duckdb() {
    let root = TempDir::new().expect("temp dir");
    let path = write_fixture(&root, 18, &[1]).0;
    let file = EnsuredQueryFile::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date(18),
        format!("../{path}"),
        1,
    );

    let error = executor(&root, 1024, 1024)
        .execute(
            select_star(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("noncanonical path");
    assert_eq!(error.code, "storage_path_not_allowed");
}

#[test]
fn executor_rejects_escaping_and_backslash_paths() {
    let root = TempDir::new().expect("temp dir");
    let path = write_fixture(&root, 18, &[1]).0;
    let query = executor(&root, 1024, 1024);

    let candidates = [
        format!("{path}\\malicious"),
        format!("/absolute/{path}"),
        "date=../asset_class=equity/data_type=trade/AAPL.parquet".to_string(),
        "date=//asset_class=equity/data_type=trade/AAPL.parquet".to_string(),
    ];
    for object_path in candidates {
        let file = EnsuredQueryFile::new(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            session_date(18),
            object_path.clone(),
            1,
        );
        let error = query
            .execute(
                select_star(),
                &[file],
                false,
                false,
                BoundedQueryOutput::Json,
            )
            .expect_err("canonical violation");
        assert_eq!(error.code, "storage_path_not_allowed", "{object_path}");
    }
}

#[test]
fn executor_force_requires_authorization_and_limits_are_validated() {
    let query = BoundedDuckDbQuery::new(".", QueryLimits::new(1024, 1024).expect("valid limits"));
    let error = query
        .execute("SELECT 1", &[], true, false, BoundedQueryOutput::Json)
        .expect_err("force not authorized");
    assert_eq!(error.code, "force_override_not_authorized");

    assert_eq!(
        QueryLimits::new(10, 11).expect_err("result limit"),
        "result limit cannot exceed scan limit"
    );
    assert_eq!(
        QueryLimits::new(0, 10).expect_err("positive scan"),
        "query byte limits must be positive"
    );
    assert_eq!(
        QueryLimits::new(10, 0).expect_err("positive result"),
        "query byte limits must be positive"
    );
}

#[test]
fn scan_cap_is_estimated_from_catalog_metadata() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let file = ensured_file(18, 100);
    let query = executor(&root, 99, 99);

    let error = query
        .execute(
            select_ordered(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("scan cap exceeded");
    assert_eq!(error.code, "scan_limit_exceeded");
    // The conservative estimate is max(catalog bytes, actual bytes); the
    // real fixture is larger than the 100-byte catalog claim.
    let actual = fixture_size(&root, 18);
    assert!(actual > 100);
    assert!(error.message.contains(&format!(
        "estimated scan of {actual} bytes exceeds the 99-byte limit"
    )));
}

#[test]
fn scan_estimate_uses_the_larger_actual_byte_size() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let actual = fixture_size(&root, 18);
    let file = ensured_file(18, 1);

    let error = executor(&root, 99, 99)
        .execute(
            select_star(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("conservative estimate wins");
    assert!(error.message.contains(&format!(
        "estimated scan of {actual} bytes exceeds the 99-byte limit"
    )));
}

#[test]
fn result_cap_is_enforced_for_selected_output() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let file = ensured_file(18, fixture_size(&root, 18));
    let query = executor(&root, 1_073_741_824, 1);

    let error = query
        .execute(
            select_ordered(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("result cap exceeded");
    assert_eq!(error.code, "result_limit_exceeded");
}

#[test]
fn arrow_result_limit_is_measured_as_arrow_bytes() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let file_size = fixture_size(&root, 18);
    let file = ensured_file(18, file_size);
    let query = BoundedDuckDbQuery::new(
        root.path(),
        QueryLimits::new(file_size, 1).expect("valid limits"),
    );

    let error = query
        .execute(
            select_star(),
            &[file],
            false,
            false,
            BoundedQueryOutput::Arrow,
        )
        .expect_err("arrow result cap exceeded");
    assert_eq!(error.code, "result_limit_exceeded");
}

#[test]
fn authorized_force_crosses_scan_and_result_caps() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let file = ensured_file(18, 1);
    let query = BoundedDuckDbQuery::new(root.path(), QueryLimits::new(1, 1).expect("valid limits"));

    let result = query
        .execute(
            select_ordered(),
            &[file],
            true,
            true,
            BoundedQueryOutput::Json,
        )
        .expect("forced result");
    assert!(result.forced);
    assert_eq!(result.rows.len(), 1);
}

#[test]
fn missing_ensured_file_is_storage_unavailable() {
    let root = TempDir::new().expect("temp dir");
    let query = executor(&root, 1_073_741_824, 33_554_432);

    let error = query
        .execute(
            select_star(),
            &[ensured_file(18, 10)],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("missing file");
    assert_eq!(error.code, "query_storage_unavailable");
    assert!(error
        .message
        .starts_with("ensured query file is unavailable: "));
}

#[test]
fn unavailable_query_root_is_storage_unavailable() {
    let root = TempDir::new().expect("temp dir");
    let query = BoundedDuckDbQuery::new(
        root.path().join("absent"),
        QueryLimits::new(1024, 1024).expect("valid limits"),
    );

    let error = query
        .execute(
            select_star(),
            &[ensured_file(18, 10)],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("missing root");
    assert_eq!(error.code, "query_storage_unavailable");
    assert_eq!(error.message, "the local query root is unavailable");
}

#[test]
fn syntax_errors_are_reported_as_rejected_sql() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let file = ensured_file(18, fixture_size(&root, 18));
    let query = executor(&root, 1_073_741_824, 33_554_432);

    let error = query
        .execute(
            "SELECT FROM data",
            &[file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect_err("invalid SQL");
    assert_eq!(error.code, "sql_not_allowed");
    assert!(error.message.starts_with("DuckDB rejected the SQL query: "));
}

#[test]
fn generator_and_unnest_relations_are_rejected() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1]);
    let file = ensured_file(18, fixture_size(&root, 18));
    let query = executor(&root, 1_073_741_824, 33_554_432);

    for sql in [
        "SELECT * FROM data CROSS JOIN range(1000)",
        "SELECT * FROM data, unnest([1, 2])",
    ] {
        let error = query
            .execute(
                sql,
                std::slice::from_ref(&file),
                false,
                false,
                BoundedQueryOutput::Json,
            )
            .expect_err("external relation");
        assert!(
            error.code == "sql_not_allowed" || error.code == "storage_path_not_allowed",
            "sql {sql}: {}",
            error.code
        );
    }
}

#[test]
fn duplicate_ensured_files_are_scanned_once() {
    let root = TempDir::new().expect("temp dir");
    let _ = write_fixture(&root, 18, &[1, 2]);
    let file = ensured_file(18, fixture_size(&root, 18));
    let query = executor(&root, 1_073_741_824, 33_554_432);

    let result = query
        .execute(
            select_star(),
            &[file.clone(), file],
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect("deduplicated scan");
    assert_eq!(result.rows.len(), 2);
}

#[test]
fn multiple_ensured_files_union_the_relation() {
    let root = TempDir::new().expect("temp dir");
    let (first_path, first_bytes) = write_fixture(&root, 18, &[1]);
    let (second_path, second_bytes) = write_fixture(&root, 19, &[2]);
    let files = [
        EnsuredQueryFile::new(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            session_date(18),
            first_path,
            first_bytes,
        ),
        EnsuredQueryFile::new(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            session_date(19),
            second_path,
            second_bytes,
        ),
    ];
    let query = executor(&root, 1_073_741_824, 33_554_432);

    let result = query
        .execute(
            "SELECT COUNT(*) AS trades FROM data",
            &files,
            false,
            false,
            BoundedQueryOutput::Json,
        )
        .expect("both partitions scanned");
    assert_eq!(result.rows.len(), 1);
    assert_eq!(result.rows[0]["trades"], json!(2));
}

#[test]
fn validator_smoke() {
    assert_eq!(
        validate_read_only_sql("SELECT trade_id, price FROM data ORDER BY trade_id")
            .expect("valid"),
        "SELECT trade_id, price FROM data ORDER BY trade_id"
    );
    let error = validate_read_only_sql("").expect_err("empty");
    assert_eq!(error.code, "sql_not_allowed");
}
