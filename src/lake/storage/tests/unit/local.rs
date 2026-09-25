use super::*;
use tempfile::TempDir;

fn store() -> (TempDir, LocalFilesystemStore) {
    let directory = tempfile::tempdir().expect("temp directory");
    let store = LocalFilesystemStore::new(directory.path());
    (directory, store)
}

const OBJECT: &str = "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet";

#[test]
fn replace_and_read_round_trip_replaces_whole_objects() {
    let (_directory, store) = store();

    store.replace(OBJECT, b"first").expect("first replace");
    assert_eq!(store.read(OBJECT).expect("read"), b"first");

    store
        .replace(OBJECT, b"second payload")
        .expect("second replace");
    assert_eq!(store.read(OBJECT).expect("read"), b"second payload");
}

#[test]
fn new_creates_the_root_directory_and_resolves_it() {
    let directory = tempfile::tempdir().expect("temp directory");
    let nested = directory.path().join("one").join("two");
    let store = LocalFilesystemStore::new(&nested);

    assert!(nested.is_dir());
    assert!(store.root().is_absolute());
}

#[test]
fn rejects_paths_that_are_not_relative_posix_objects() {
    let (_directory, store) = store();
    for path in [
        "",
        "/absolute/file",
        "a\\b",
        "../escape",
        "a/../b",
        "a/./b",
        "a//b",
        "./file",
        "trailing/",
        "..",
    ] {
        let error = store.replace(path, b"payload").expect_err(path);
        match error {
            DataError::ValueError(message) => {
                assert_eq!(
                    message,
                    "object path must be a non-empty relative POSIX path"
                );
            }
            other => panic!("expected ValueError for {path:?}, got {other:?}"),
        }
        assert!(store.read(path).is_err(), "read accepted {path:?}");
    }
}

#[test]
fn failed_replacement_removes_the_temporary_file() {
    let (_directory, store) = store();
    let blocked = store.root().join("blocked");
    std::fs::create_dir_all(&blocked).expect("blocked directory");
    std::fs::write(blocked.join("keep"), b"x").expect("keep file");

    let error = store
        .replace("blocked", b"payload")
        .expect_err("replace must fail");
    assert!(
        matches!(error, DataError::Io(_)),
        "unexpected error: {error:?}"
    );

    let leftovers: Vec<String> = std::fs::read_dir(store.root())
        .expect("root listing")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|name| name.ends_with(".tmp"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "leftover temporary files: {leftovers:?}"
    );
    assert!(blocked.join("keep").is_file());
}

#[test]
fn prefix_listing_is_sorted_and_deletion_counts_files() {
    let (_directory, store) = store();
    store
        .replace(
            "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet",
            b"1",
        )
        .expect("write");
    store
        .replace(
            "date=2024-01-19/asset_class=equity/data_type=quote/AAPL.parquet",
            b"2",
        )
        .expect("write");
    store
        .replace(
            "date=2024-01-20/asset_class=equity/data_type=trade/AAPL.parquet",
            b"3",
        )
        .expect("write");

    let listed = store.list_prefix("date=2024-01-19").expect("list");
    assert_eq!(
        listed,
        vec![
            "date=2024-01-19/asset_class=equity/data_type=quote/AAPL.parquet".to_string(),
            "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet".to_string(),
        ]
    );
    assert_eq!(store.list_prefix("").expect("list all").len(), 3);
    assert!(store
        .list_prefix("missing")
        .expect("list missing")
        .is_empty());

    assert_eq!(store.delete_prefix("date=2024-01-19").expect("delete"), 2);
    assert!(store
        .list_prefix("date=2024-01-19")
        .expect("list")
        .is_empty());
    assert_eq!(store.list_prefix("").expect("list all").len(), 1);
}
