//! Entry-point flip coverage.
//!
//! T278 flips the Python console scripts onto the Rust binaries. The native
//! `backtest-job` engine is wired through the JIT seam and proven in
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
    run_help("screen-job", env!("CARGO_BIN_EXE_screen-job"));
}

#[test]
fn rust_api_and_mcp_entrypoints_run() {
    run_help("api", env!("CARGO_BIN_EXE_api"));
    run_help("mcp", env!("CARGO_BIN_EXE_mcp"));
}

/// The flipped `backtest-job` binary drives the native engine end to end and
/// prints the canonical summary for the committed configuration over a real
/// lake route.
#[test]
fn backtest_job_binary_runs_the_native_engine() {
    use backtest::backtest::errors::ONE_DAY_NS;
    use harness_lakeclient::testing::{
        quote_stream, trade_stream, FakeLakeConfig, FakeLakeServer, FakeQuoteRow, FakeTradeRow,
    };
    use marketdata::Date;
    use std::collections::BTreeMap;

    const TOKEN: &str = "entrypoint-lake-token";

    let mut quotes = Vec::new();
    let mut trades = Vec::new();
    for day in 0..3u32 {
        let date = Date::from_ymd(2024, 1, 2 + day).expect("date");
        let base = i64::from(date.days()) * ONE_DAY_NS + (13 * 3_600 + 30 * 60) * 1_000_000_000;
        for step in 0..12i64 {
            let mid = 100.00 + 0.02 * step as f64;
            quotes.push(FakeQuoteRow::new(
                base + (32 * 60 * step) * 1_000_000_000,
                &format!("{:.2}", mid - 0.01),
                &format!("{:.2}", mid + 0.01),
                100,
                100,
                "XNYS",
            ));
        }
        trades.push(FakeTradeRow::new(
            base + 60 * 1_000_000_000,
            i64::from(day) as u64 + 1,
            "100.02",
            20,
            "XNYS",
        ));
    }
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([
            ("quote".to_string(), quote_stream("SPY", &quotes)),
            ("trade".to_string(), trade_stream("SPY", &trades)),
        ]),
        ..FakeLakeConfig::new(TOKEN)
    });

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
        .env("LAKE_API_URL", server.base_url())
        .env("LAKE_API__BEARER_TOKEN", TOKEN)
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
    assert!(
        stdout.contains("job_id: job-momentum-msft-2024-01"),
        "{stdout}"
    );
    let _ = std::fs::remove_dir_all(&output_dir);
}
