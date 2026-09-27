//! Local command wiring: bearer, layout, credential, and reset behavior.

mod support;

use std::collections::BTreeMap;
use std::fs;

use lake_cli::execute;
use lake_local::LocalLayout;
use support::{arguments, environment, TempDir};

fn empty() -> BTreeMap<String, String> {
    BTreeMap::new()
}

#[test]
fn local_api_requires_a_non_empty_bearer_before_binding() {
    let temporary = TempDir::new("api-bearer");
    let data_dir = temporary.path().join("local");
    let data_dir_text = data_dir.to_string_lossy().into_owned();
    let whitespace = environment(&[("LAKE_API__BEARER_TOKEN", "   ")]);

    for environ in [empty(), whitespace] {
        let outcome = execute(
            &arguments(&["local", "api", "--data-dir", &data_dir_text]),
            &environ,
        );
        assert_eq!(outcome.code, 2, "stderr: {}", outcome.stderr);
        assert!(outcome.stdout.is_empty());
        assert!(
            outcome.stderr.starts_with("lake local: error: "),
            "stderr: {}",
            outcome.stderr
        );
        assert!(
            outcome
                .stderr
                .contains("LAKE_API__BEARER_TOKEN is required"),
            "stderr: {}",
            outcome.stderr
        );
    }
    assert!(
        !data_dir.exists(),
        "no local state is created before binding"
    );
}

#[test]
fn local_ingest_requires_provider_credentials_before_touching_state() {
    let temporary = TempDir::new("ingest-credentials");
    let data_dir = temporary.path().join("local");
    let data_dir_text = data_dir.to_string_lossy().into_owned();

    let outcome = execute(
        &arguments(&[
            "local",
            "ingest",
            "--symbol",
            "AAPL",
            "--data-dir",
            &data_dir_text,
        ]),
        &empty(),
    );

    assert_eq!(outcome.code, 2, "stderr: {}", outcome.stderr);
    assert!(outcome.stdout.is_empty());
    assert!(
        outcome.stderr.starts_with("lake local: error: "),
        "stderr: {}",
        outcome.stderr
    );
    assert!(
        outcome.stderr.contains("LAKE_PROVIDER__API_KEY"),
        "stderr: {}",
        outcome.stderr
    );
    assert!(
        outcome.stderr.contains("LAKE_PROVIDER__API_SECRET"),
        "stderr: {}",
        outcome.stderr
    );
    assert!(
        !data_dir.exists(),
        "credentials fail before layout creation"
    );
}

#[test]
fn local_ingest_reports_session_selection_errors_with_credentials() {
    let temporary = TempDir::new("ingest-session");
    let data_dir = temporary.path().join("local");
    let data_dir_text = data_dir.to_string_lossy().into_owned();
    let environ = environment(&[
        ("LAKE_PROVIDER__API_KEY", "fake-key"),
        ("LAKE_PROVIDER__API_SECRET", "fake-secret"),
    ]);

    let outcome = execute(
        &arguments(&[
            "local",
            "ingest",
            "--symbol",
            "AAPL",
            "--session-date",
            "2026-09-05",
            "--data-dir",
            &data_dir_text,
        ]),
        &environ,
    );

    assert_eq!(outcome.code, 2, "stderr: {}", outcome.stderr);
    assert!(
        outcome.stderr.contains("closed XNYS session"),
        "stderr: {}",
        outcome.stderr
    );
}

#[test]
fn local_ingest_rejects_a_malformed_session_date() {
    let outcome = execute(
        &arguments(&["local", "ingest", "--session-date", "not-a-date"]),
        &empty(),
    );
    assert_eq!(outcome.code, 2);
    assert!(
        outcome.stderr.contains("invalid session date"),
        "stderr: {}",
        outcome.stderr
    );
}

#[test]
fn local_reset_requires_yes() {
    let temporary = TempDir::new("reset-confirm");
    let data_dir = temporary.path().join("local");
    LocalLayout::new(&data_dir)
        .initialize()
        .expect("initialize");
    let data_dir_text = data_dir.to_string_lossy().into_owned();

    let outcome = execute(
        &arguments(&["local", "reset", "--data-dir", &data_dir_text]),
        &empty(),
    );

    assert_eq!(outcome.code, 2);
    assert_eq!(outcome.stderr, "lake local: error: reset requires --yes\n");
    assert!(outcome.stdout.is_empty());
    assert!(data_dir.exists(), "state survives a refused reset");
}

#[test]
fn local_reset_removes_initialized_state_and_prints_json() {
    let temporary = TempDir::new("reset-remove");
    let data_dir = temporary.path().join("local");
    LocalLayout::new(&data_dir)
        .initialize()
        .expect("initialize");
    let data_dir_text = data_dir.to_string_lossy().into_owned();

    let outcome = execute(
        &arguments(&["local", "reset", "--yes", "--data-dir", &data_dir_text]),
        &empty(),
    );

    assert_eq!(outcome.code, 0, "stderr: {}", outcome.stderr);
    assert!(outcome.stderr.is_empty());
    assert!(!data_dir.exists(), "the initialized tree is removed");
    let payload: serde_json::Value =
        serde_json::from_str(outcome.stdout.trim()).expect("compact JSON payload");
    assert_eq!(payload["data_root"], serde_json::Value::from(data_dir_text));
    assert_eq!(payload["removed"], serde_json::Value::Bool(true));
}

#[test]
fn local_reset_honors_the_environment_data_dir() {
    let temporary = TempDir::new("reset-environment");
    let data_dir = temporary.path().join("from-environment");
    let data_dir_text = data_dir.to_string_lossy().into_owned();
    let environ = environment(&[("LAKE_LOCAL_DATA_DIR", &data_dir_text)]);
    LocalLayout::from_environment(None, &environ)
        .expect("layout from environment")
        .initialize()
        .expect("initialize");

    let outcome = execute(&arguments(&["local", "reset", "--yes"]), &environ);

    assert_eq!(outcome.code, 0, "stderr: {}", outcome.stderr);
    assert!(!data_dir.exists());
}

#[test]
fn local_reset_refuses_unowned_directories() {
    let temporary = TempDir::new("reset-unowned");
    let data_dir = temporary.path().join("not-ours");
    fs::create_dir_all(&data_dir).expect("create directory");
    fs::write(data_dir.join("keep.txt"), b"keep").expect("keep file");
    let data_dir_text = data_dir.to_string_lossy().into_owned();

    let outcome = execute(
        &arguments(&["local", "reset", "--yes", "--data-dir", &data_dir_text]),
        &empty(),
    );

    assert_eq!(outcome.code, 2, "stderr: {}", outcome.stderr);
    assert!(
        outcome.stderr.contains("without the local-mode marker"),
        "stderr: {}",
        outcome.stderr
    );
    assert!(data_dir.join("keep.txt").exists());
}

#[test]
fn local_reset_rejects_an_empty_environment_override() {
    let environ = environment(&[("LAKE_LOCAL_DATA_DIR", "   ")]);

    let outcome = execute(&arguments(&["local", "reset", "--yes"]), &environ);

    assert_eq!(outcome.code, 2);
    assert!(
        outcome.stderr.contains("must not be empty"),
        "stderr: {}",
        outcome.stderr
    );
}
