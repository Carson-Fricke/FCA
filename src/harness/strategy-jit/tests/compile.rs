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
    assert!(workspace
        .target_dir()
        .join(&entry.artifact_relpath)
        .is_file());
    let _ = std::fs::remove_dir_all(&directory);
}
