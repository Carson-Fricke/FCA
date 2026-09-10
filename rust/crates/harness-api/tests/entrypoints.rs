//! Entry-point flip coverage.
//!
//! T278 prepares the flip of the Python console scripts onto the Rust
//! binaries. The Rust engine is not fully wired yet (`backtest-job` and
//! `trader` still drive the injected seam), so the flip is behind the
//! `HARNESS_USE_RUST_ENTRYPOINTS` flag and only `api` and `mcp` are flipped.
//! This test proves all four Rust process entry points exist and run.

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
    assert!(!stdout.trim().is_empty(), "{name} --help produced no output");
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
