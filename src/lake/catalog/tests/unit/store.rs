use super::*;
use crate::models::CoverageStatus;

const AAPL_ROW: &str = concat!(
    "{\"asset_class\":\"equity\",\"byte_size\":3456,\"checksum\":\"sha256:abc\",",
    "\"data_type\":\"trade\",\"error_message\":null,",
    "\"object_path\":\"date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet\",",
    "\"row_count\":12,\"session_date\":\"2024-01-19\",\"status\":\"complete\",",
    "\"symbol\":\"AAPL\"}"
);

const MSFT_ROW: &str = concat!(
    "{\"asset_class\":\"equity\",\"byte_size\":0,\"checksum\":null,",
    "\"data_type\":\"quote\",\"error_message\":null,\"object_path\":null,",
    "\"row_count\":0,\"session_date\":\"2024-01-18\",\"status\":\"empty\",\"symbol\":\"MSFT\"}"
);

/// A realistic `catalog.json` as Python writes it: pretty spacing, a
/// non-canonical row order, and `ensure_ascii` `\u` escapes.
const PYTHON_CATALOG: &str = concat!(
    "{\n",
    "  \"coverage\": [\n",
    "    {\n",
    "      \"asset_class\": \"equity\",\n",
    "      \"byte_size\": 64,\n",
    "      \"checksum\": null,\n",
    "      \"data_type\": \"trade\",\n",
    "      \"error_message\": \"source unavailable caf\\u00e9 \\u007f / \\u00e9\",\n",
    "      \"object_path\": null,\n",
    "      \"row_count\": 3,\n",
    "      \"session_date\": \"2024-01-22\",\n",
    "      \"status\": \"error\",\n",
    "      \"symbol\": \"WB\"\n",
    "    },\n",
    "    {\n",
    "      \"asset_class\": \"equity\",\n",
    "      \"byte_size\": 0,\n",
    "      \"checksum\": null,\n",
    "      \"data_type\": \"quote\",\n",
    "      \"error_message\": null,\n",
    "      \"object_path\": null,\n",
    "      \"row_count\": 0,\n",
    "      \"session_date\": \"2024-01-18\",\n",
    "      \"status\": \"empty\",\n",
    "      \"symbol\": \"MSFT\"\n",
    "    },\n",
    "    {\n",
    "      \"asset_class\": \"equity\",\n",
    "      \"byte_size\": 3456,\n",
    "      \"checksum\": \"sha256:abc\",\n",
    "      \"data_type\": \"trade\",\n",
    "      \"error_message\": null,\n",
    "      \"object_path\": \"date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet\",\n",
    "      \"row_count\": 12,\n",
    "      \"session_date\": \"2024-01-19\",\n",
    "      \"status\": \"complete\",\n",
    "      \"symbol\": \"AAPL\"\n",
    "    }\n",
    "  ],\n",
    "  \"schema_version\": 1\n",
    "}\n"
);

/// Exactly what Python's canonical `json.dumps` call emits for the fixture,
/// coverage rows sorted by `(asset_class, data_type, symbol, session_date)`.
const CANONICAL_CATALOG: &str = concat!(
    "{\"coverage\":[",
    "{\"asset_class\":\"equity\",\"byte_size\":0,\"checksum\":null,",
    "\"data_type\":\"quote\",\"error_message\":null,\"object_path\":null,",
    "\"row_count\":0,\"session_date\":\"2024-01-18\",\"status\":\"empty\",\"symbol\":\"MSFT\"},",
    "{\"asset_class\":\"equity\",\"byte_size\":3456,\"checksum\":\"sha256:abc\",",
    "\"data_type\":\"trade\",\"error_message\":null,",
    "\"object_path\":\"date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet\",",
    "\"row_count\":12,\"session_date\":\"2024-01-19\",\"status\":\"complete\",",
    "\"symbol\":\"AAPL\"},",
    "{\"asset_class\":\"equity\",\"byte_size\":64,\"checksum\":null,",
    "\"data_type\":\"trade\",\"error_message\":\"source unavailable caf\\u00e9 \\u007f / \\u00e9\",",
    "\"object_path\":null,\"row_count\":3,\"session_date\":\"2024-01-22\",",
    "\"status\":\"error\",\"symbol\":\"WB\"}",
    "],\"schema_version\":1}\n"
);

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("date")
}

fn coverage(symbol: &str, status: CoverageStatus) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        symbol,
        day(2024, 1, 19),
        status,
        None,
        1,
        7,
        None,
        None,
    )
    .expect("valid coverage")
}

fn aapl() -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        day(2024, 1, 19),
        CoverageStatus::Complete,
        Some("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet".to_string()),
        12,
        3456,
        Some("sha256:abc".to_string()),
        None,
    )
    .expect("valid coverage")
}

fn msft() -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Quote,
        "MSFT",
        day(2024, 1, 18),
        CoverageStatus::Empty,
        None,
        0,
        0,
        None,
        None,
    )
    .expect("valid coverage")
}

fn wb() -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "WB",
        day(2024, 1, 22),
        CoverageStatus::Error,
        None,
        3,
        64,
        None,
        Some("source unavailable caf\u{e9} \u{7f} / \u{e9}".to_string()),
    )
    .expect("valid coverage")
}

fn temp_directory(label: &str) -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let path = std::env::temp_dir().join(format!(
        "lake-catalog-{label}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).expect("temp dir");
    path
}

fn cleanup(path: &Path) {
    let _ = fs::remove_dir_all(path);
}

fn symbols(records: &[CoverageRecord]) -> Vec<&str> {
    records
        .iter()
        .map(|record| record.symbol.as_str())
        .collect()
}

#[test]
fn migration_reads_python_catalog_and_reserializes_exactly() {
    let directory = temp_directory("migration");
    let path = directory.join("catalog.json");
    fs::write(&path, PYTHON_CATALOG).expect("seed");

    let store = JsonCatalogStore::open(&path).expect("open");
    assert_eq!(store.path(), Some(path.as_path()));
    let listed = store.list(&CoverageQuery::default()).expect("list");
    assert_eq!(symbols(&listed), ["MSFT", "AAPL", "WB"]);
    assert_eq!(
        store
            .get(
                AssetClass::Equity,
                DataType::Trade,
                "aapl",
                day(2024, 1, 19)
            )
            .expect("get"),
        Some(aapl())
    );
    assert_eq!(
        store
            .get(
                AssetClass::Equity,
                DataType::Trade,
                "AAPL",
                day(2024, 1, 18)
            )
            .expect("get"),
        None
    );

    let records = [wb(), msft(), aapl()];
    store.upsert_many(records.iter()).expect("upsert many");
    assert_eq!(fs::read_to_string(&path).expect("read"), CANONICAL_CATALOG);
    let leftovers: Vec<String> = fs::read_dir(&directory)
        .expect("dir")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".tmp"))
        .collect();
    assert!(leftovers.is_empty(), "leftover temp files: {leftovers:?}");
    cleanup(&directory);
}

#[test]
fn missing_file_is_an_empty_store_that_creates_the_file_on_write() {
    let directory = temp_directory("missing");
    let path = directory.join("state").join("catalog.json");

    let store = JsonCatalogStore::open(&path).expect("open");
    assert_eq!(store.list(&CoverageQuery::default()).expect("list"), vec![]);
    assert!(!path.exists());

    store.upsert(&aapl()).expect("upsert");
    let expected = format!("{{\"coverage\":[{AAPL_ROW}],\"schema_version\":1}}\n");
    assert_eq!(fs::read_to_string(&path).expect("read"), expected);
    assert_eq!(
        JsonCatalogStore::open(&path)
            .expect("reopen")
            .list(&CoverageQuery::default())
            .expect("list"),
        vec![aapl()]
    );
    cleanup(&directory);
}

#[test]
fn in_memory_store_never_touches_the_filesystem() {
    let store = JsonCatalogStore::in_memory();
    assert_eq!(store.path(), None);
    store
        .upsert(&coverage("AAPL", CoverageStatus::Partial))
        .expect("upsert");
    assert_eq!(
        store.list(&CoverageQuery::default()).expect("list").len(),
        1
    );
    store.reload().expect("reload is a no-op");
    assert_eq!(
        store
            .get(
                AssetClass::Equity,
                DataType::Trade,
                "AAPL",
                day(2024, 1, 19)
            )
            .expect("get"),
        Some(coverage("AAPL", CoverageStatus::Partial))
    );
}

#[test]
fn malformed_documents_are_rejected_without_overwriting() {
    let cases: Vec<(String, &str)> = vec![
        (
            "{\"coverage\":[],\"schema_version\":2}".to_string(),
            "catalog schema version is unsupported",
        ),
        (
            "{\"coverage\":[],\"schema_version\":1,\"credentials\":\"x\"}".to_string(),
            "catalog root has unexpected fields",
        ),
        (
            "{\"coverage\":{},\"schema_version\":1}".to_string(),
            "catalog coverage must be a list",
        ),
        (
            concat!(
                "{\"coverage\":[{\"asset_class\":\"equity\",\"byte_size\":0,",
                "\"checksum\":null,\"data_type\":\"trade\",\"error_message\":null,",
                "\"object_path\":null,\"row_count\":0,\"session_date\":\"2024-01-19\",",
                "\"status\":\"empty\",\"symbol\":\"AAPL\",\"database\":\"x\"}],",
                "\"schema_version\":1}"
            )
            .to_string(),
            "coverage row has unexpected fields",
        ),
        (
            concat!(
                "{\"coverage\":[{\"asset_class\":\"equity\",\"byte_size\":0,",
                "\"checksum\":null,\"data_type\":\"trade\",\"error_message\":null,",
                "\"object_path\":null,\"row_count\":0,\"session_date\":\"2024-01-19\",",
                "\"status\":\"empty\"}],\"schema_version\":1}"
            )
            .to_string(),
            "coverage row has unexpected fields",
        ),
        (
            format!("{{\"coverage\":[{AAPL_ROW},{AAPL_ROW}],\"schema_version\":1}}"),
            "catalog contains duplicate coverage keys",
        ),
    ];
    for (index, (document, message)) in cases.iter().enumerate() {
        let directory = temp_directory(&format!("invalid-{index}"));
        let path = directory.join("catalog.json");
        fs::write(&path, document).expect("seed");
        let error = JsonCatalogStore::open(&path).expect_err("reject");
        let text = error.to_string();
        assert!(
            text.contains("local catalog is invalid") && text.contains(message),
            "unexpected error for case {index}: {text}"
        );
        assert_eq!(fs::read_to_string(&path).expect("read"), *document);
        cleanup(&directory);
    }
}

#[test]
fn upsert_many_rejects_duplicate_keys_before_writing() {
    let directory = temp_directory("duplicates");
    let path = directory.join("catalog.json");
    fs::write(&path, PYTHON_CATALOG).expect("seed");
    let store = JsonCatalogStore::open(&path).expect("open");

    let error = store
        .upsert_many([&aapl(), &aapl()])
        .expect_err("duplicate keys");
    assert_eq!(
        error,
        DataError::value("coverage update contains duplicate keys")
    );
    assert_eq!(fs::read_to_string(&path).expect("read"), PYTHON_CATALOG);
    cleanup(&directory);
}

#[test]
fn reload_sees_external_writes_and_reads_notice_a_changed_stamp() {
    let directory = temp_directory("reload");
    let path = directory.join("catalog.json");
    let store = JsonCatalogStore::open(&path).expect("open");
    store.upsert(&aapl()).expect("upsert");
    assert_eq!(
        store.list(&CoverageQuery::default()).expect("list").len(),
        1
    );

    std::thread::sleep(Duration::from_millis(30));
    let external = format!("{{\"coverage\":[{MSFT_ROW},{AAPL_ROW}],\"schema_version\":1}}\n");
    fs::write(&path, &external).expect("external write");
    assert_eq!(
        symbols(&store.list(&CoverageQuery::default()).expect("list")),
        ["MSFT", "AAPL"],
        "reads stat-check the file and reload on a changed (mtime, size) stamp"
    );

    store.reload().expect("second reload is a stamp no-op");
    assert_eq!(
        symbols(&store.list(&CoverageQuery::default()).expect("list")),
        ["MSFT", "AAPL"]
    );
    cleanup(&directory);
}

#[test]
fn list_sorts_by_persisted_values_and_rejects_reversed_ranges() {
    let store = JsonCatalogStore::in_memory();
    let records = [
        CoverageRecord::new(
            AssetClass::Reference,
            DataType::Trade,
            "AAPL",
            day(2024, 1, 19),
            CoverageStatus::Empty,
            None,
            0,
            0,
            None,
            None,
        )
        .expect("valid"),
        CoverageRecord::new(
            AssetClass::Option,
            DataType::Trade,
            "AAPL",
            day(2024, 1, 19),
            CoverageStatus::Empty,
            None,
            0,
            0,
            None,
            None,
        )
        .expect("valid"),
        CoverageRecord::new(
            AssetClass::Crypto,
            DataType::Trade,
            "AAPL",
            day(2024, 1, 19),
            CoverageStatus::Empty,
            None,
            0,
            0,
            None,
            None,
        )
        .expect("valid"),
        coverage("AAPL", CoverageStatus::Empty),
    ];
    store.upsert_many(&records).expect("upsert");

    let query = CoverageQuery::default();
    let listed = store.list(&query).expect("list");
    let classes: Vec<&str> = listed
        .iter()
        .map(|record| record.asset_class.value())
        .collect();
    assert_eq!(classes, ["crypto", "equity", "option", "reference"]);

    let reversed = CoverageQuery {
        start: Some(day(2024, 1, 2)),
        end: Some(day(2024, 1, 1)),
        ..CoverageQuery::default()
    };
    assert_eq!(
        store.list(&reversed).expect_err("reversed"),
        DataError::value("start must be on or before end")
    );
}

#[cfg(windows)]
#[test]
fn atomic_replace_fails_while_a_lock_is_held_and_recovers_after_release() {
    use std::os::windows::fs::OpenOptionsExt;

    let directory = temp_directory("replace-retry");
    let path = directory.join("catalog.json");
    fs::write(&path, b"old").expect("seed");
    let held = OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(&path)
        .expect("exclusive open");

    let started = std::time::Instant::now();
    write_atomic(&path, b"new").expect_err("held lock denies every replace attempt");
    let elapsed = started.elapsed();
    // Six attempts with 50ms * attempt backoff must have been burned.
    assert!(
        elapsed >= Duration::from_millis(300),
        "expected the retry window to elapse, got {elapsed:?}"
    );

    drop(held);
    assert_eq!(fs::read(&path).expect("read"), b"old");
    write_atomic(&path, b"new").expect("replace succeeds after the lock is released");
    assert_eq!(fs::read(&path).expect("read"), b"new");
    let leftovers: Vec<String> = fs::read_dir(&directory)
        .expect("dir")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".tmp"))
        .collect();
    assert!(leftovers.is_empty(), "leftover temp files: {leftovers:?}");
    cleanup(&directory);
}
