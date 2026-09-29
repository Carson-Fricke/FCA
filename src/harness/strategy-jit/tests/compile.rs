//! Managed-workspace compilation behavior.

use std::path::PathBuf;

use harness_strategy_jit::{compile, ManagedWorkspace, ProgramKind};

const STUDY_SOURCE: &str = include_str!("fixtures/study_program.rs");

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-strategy-jit-compile-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

#[test]
fn compile_failures_report_the_build_error() {
    let directory = temp_dir("failure");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let error =
        compile(&workspace, "this is not rust", ProgramKind::Study).expect_err("must not compile");
    match error {
        harness_strategy_jit::JitError::Compile { detail, .. } => {
            assert!(
                detail.contains("error"),
                "expected the compiler detail, got {detail}"
            );
        }
        other => panic!("expected a compile failure, got {other:?}"),
    }
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn an_empty_source_is_rejected_before_any_build() {
    let directory = temp_dir("empty-source");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let error = compile(&workspace, "   \n", ProgramKind::Strategy).expect_err("empty source");
    assert!(
        matches!(error, harness_strategy_jit::JitError::Invalid(_)),
        "expected an invalid-source error, got {error:?}"
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn the_workspace_records_provenance_for_every_artifact() {
    let directory = temp_dir("manifest");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let compiled = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("compile");
    let entries = workspace.read_manifest();
    let entry = entries.get(&compiled.digest).expect("manifest entry");
    assert_eq!(entry.kind, "study");
    assert_eq!(entry.crate_name, compiled.crate_name);
    assert_eq!(entry.toolchain_digest, compiled.toolchain_digest);
    assert_eq!(entry.sdk_version, harness_strategy_sdk::SDK_VERSION);
    assert_eq!(entry.sdk_abi_version, harness_strategy_sdk::SDK_ABI_VERSION);
    assert!(workspace
        .target_dir()
        .join(&entry.artifact_relpath)
        .is_file());
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn a_matching_manifest_is_a_cache_hit() {
    let directory = temp_dir("cache-hit");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let first = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("initial compile");
    assert!(!first.cached);
    let second = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("cached compile");
    assert!(second.cached);
    assert_eq!(second.digest, first.digest);
    assert_eq!(second.artifact_path, first.artifact_path);
    assert_eq!(
        second.sdk_version,
        harness_strategy_sdk::SDK_VERSION.to_string()
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn an_older_manifest_abi_version_forces_a_rebuild() {
    let directory = temp_dir("stale-abi");
    let workspace = ManagedWorkspace::open(&directory).expect("workspace");
    let first = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("initial compile");
    assert!(!first.cached);

    let mut entries = workspace.read_manifest();
    let entry = entries.get_mut(&first.digest).expect("manifest entry");
    entry.sdk_abi_version = harness_strategy_sdk::SDK_ABI_VERSION - 1;
    workspace.write_manifest(&entries).expect("manifest write");

    let rebuilt = compile(&workspace, STUDY_SOURCE, ProgramKind::Study).expect("rebuild");
    assert!(!rebuilt.cached);
    assert_eq!(rebuilt.digest, first.digest);
    let entries = workspace.read_manifest();
    let entry = entries.get(&first.digest).expect("manifest entry");
    assert_eq!(entry.sdk_abi_version, harness_strategy_sdk::SDK_ABI_VERSION);
    let _ = std::fs::remove_dir_all(&directory);
}
