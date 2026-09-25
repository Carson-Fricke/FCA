//! Layout marker, initialization, and reset tests.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use lake_local::{LocalFileIssuer, LocalLayout, MARKER_CONTENT, MARKER_NAME};
use lake_query::FileUrlIssuer;

fn environ_with(path: &Path) -> BTreeMap<String, String> {
    BTreeMap::from([(
        "LAKE_LOCAL_DATA_DIR".to_string(),
        path.to_string_lossy().into_owned(),
    )])
}

#[test]
fn layout_uses_environment_and_reset_requires_ownership_marker() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let configured = temporary.path().join("lake-data");
    let layout = LocalLayout::from_environment(None, &environ_with(&configured))
        .expect("layout from environment");
    assert!(!layout.is_initialized());

    layout.initialize().expect("initialize");

    assert_eq!(layout.root(), configured.as_path());
    assert!(layout.objects().is_dir());
    assert!(layout.state_dir().is_dir());
    assert_eq!(
        fs::read_to_string(layout.marker_path()).expect("marker"),
        MARKER_CONTENT
    );
    assert!(layout.is_initialized());
    layout.reset().expect("reset");
    assert!(!configured.exists());
    // Resetting an absent tree is a no-op.
    layout.reset().expect("reset absent");

    let unrelated = temporary.path().join("unrelated");
    fs::create_dir(&unrelated).expect("unrelated dir");
    fs::write(unrelated.join("keep.txt"), b"keep").expect("keep file");
    let unsafe_layout = LocalLayout::new(&unrelated);
    let error = unsafe_layout
        .initialize()
        .expect_err("refuse non-empty dir");
    assert!(error.message.contains("not created by local mode"));
    let error = unsafe_layout.reset().expect_err("refuse unmarked reset");
    assert!(error.message.contains("without the local-mode marker"));
    assert_eq!(
        fs::read_to_string(unrelated.join("keep.txt")).expect("keep read"),
        "keep"
    );
}

#[test]
fn layout_rejects_wrong_markers_non_directories_and_empty_overrides() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let root = temporary.path().join("local");
    fs::create_dir_all(&root).expect("root");
    fs::write(root.join(MARKER_NAME), b"wrong\n").expect("wrong marker");
    let layout = LocalLayout::new(&root);
    let error = layout.initialize().expect_err("wrong marker init");
    assert!(error.message.contains("invalid local data marker"));
    let error = layout.reset().expect_err("wrong marker reset");
    assert!(error.message.contains("invalid local data marker"));

    let file_root = temporary.path().join("file-root");
    fs::write(&file_root, b"data").expect("file root");
    let error = LocalLayout::new(&file_root)
        .initialize()
        .expect_err("file root");
    assert!(error.message.contains("not a directory"));

    let environ = BTreeMap::from([("LAKE_LOCAL_DATA_DIR".to_string(), "   ".to_string())]);
    let error = LocalLayout::from_environment(None, &environ).expect_err("empty override");
    assert!(error.message.contains("must not be empty"));
}

#[test]
fn explicit_data_dir_wins_and_the_default_is_the_repository_local_directory() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let explicit = temporary.path().join("explicit");
    let from_environment = temporary.path().join("from-environment");
    let layout = LocalLayout::from_environment(Some(&explicit), &environ_with(&from_environment))
        .expect("explicit layout");
    assert_eq!(layout.root(), explicit.as_path());

    let default = LocalLayout::from_environment(None, &BTreeMap::new()).expect("default layout");
    assert!(default
        .root()
        .ends_with(Path::new(".local").join("market-lake")));
}

#[test]
fn file_issuer_confines_urls_to_existing_objects() {
    let temporary = tempfile::tempdir().expect("tempdir");
    let root = temporary.path().join("objects");
    let target = root.join("date=2024-01-19").join("AAPL.parquet");
    fs::create_dir_all(target.parent().expect("parent")).expect("object dir");
    fs::write(&target, b"parquet").expect("object");
    let issuer = LocalFileIssuer::new(&root);

    let url = issuer.issue("date=2024-01-19/AAPL.parquet").expect("issue");
    assert!(url.starts_with("file://"));
    assert!(url.ends_with("/date=2024-01-19/AAPL.parquet"));
    let error = issuer.issue("../secret").expect_err("escape");
    assert!(error.to_string().contains("canonical"));
    let error = issuer
        .issue("date=2024-01-19/MISSING.parquet")
        .expect_err("missing");
    assert!(error.to_string().contains("unavailable"));
}
