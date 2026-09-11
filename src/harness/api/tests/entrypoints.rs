//! Entry-point flip coverage.
//!
//! T278 flips the Python console scripts onto the Rust binaries. The native
//! `backtest-job` engine is wired through `harness-host` and parity-proven in
//! `harness-backtest/tests/parity.rs`; `trader` remains on Python until its
//! supervision runner is ported. This test proves all four Rust process entry
//! points exist and run.

use std::path::PathBuf;
use std::process::Command;

fn run_help(name: &str, path: &str) {
    let output = Command::new(path)
        .arg("--help")
        .output()
        .unwrap_or_else(|error| panic!("{name}: could not spawn {path}: {error}"));
    assert!(
        output.status.success(),
        "{name} --help exited with {:?}: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        !stdout.trim().is_empty(),
        "{name} --help produced no output"
    );
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
}

#[test]
fn rust_facade_and_backtest_entrypoints_run() {
    run_help("harness", env!("CARGO_BIN_EXE_harness"));
    run_help("backtest-job", env!("CARGO_BIN_EXE_backtest-job"));
}

#[test]
fn rust_api_and_mcp_entrypoints_run() {
    run_help("api", env!("CARGO_BIN_EXE_api"));
    run_help("mcp", env!("CARGO_BIN_EXE_mcp"));
}

/// The flipped `backtest-job` binary drives the native engine end to end and
/// prints the canonical summary for the committed benchmark configuration.
#[test]
fn backtest_job_binary_runs_the_native_engine() {
    let config = repo_root()
        .join("config")
        .join("harness")
        .join("backtest.yaml");
    let output_dir = std::env::temp_dir().join(format!(
        "harness-api-backtest-job-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    let output = Command::new(env!("CARGO_BIN_EXE_backtest-job"))
        .arg("--config")
        .arg(&config)
        .arg("--output-dir")
        .arg(&output_dir)
        .output()
        .expect("spawn backtest-job");
    assert!(
        output.status.success(),
        "backtest-job exited with {:?}: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("status: completed"), "{stdout}");
    assert!(stdout.contains("run_id: momentum-spy-2024-01"), "{stdout}");
    assert!(stdout.contains("fills: 26"), "{stdout}");
    assert!(output_dir.join("backtest.json").is_file());
    let _ = std::fs::remove_dir_all(&output_dir);
}
