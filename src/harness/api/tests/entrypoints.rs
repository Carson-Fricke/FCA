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
/// prints the canonical summary for the committed configuration over a real
/// lake.
#[test]
fn backtest_job_binary_runs_the_native_engine() {
    use backtest::backtest::errors::ONE_DAY_NS;
    use marketdata::events::{MarketEvent as LakeEvent, QuoteEvent, TradeEvent};
    use marketdata::time::DateTime;
    use marketdata::{write_lake_partition, AssetClass, DataType, Date};

    fn dec(value: &str) -> num::Decimal18 {
        value.parse().unwrap()
    }

    let lake = std::env::temp_dir().join(format!(
        "harness-api-entrypoint-lake-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&lake);
    std::fs::create_dir_all(&lake).expect("lake dir");
    for day in 0..3u32 {
        let date = Date::from_ymd(2024, 1, 2 + day).expect("date");
        let base = i64::from(date.days()) * ONE_DAY_NS + (13 * 3_600 + 30 * 60) * 1_000_000_000;
        let mut quotes = Vec::new();
        for step in 0..12i64 {
            let mid = 100.00 + 0.02 * step as f64;
            quotes.push(LakeEvent::Quote(QuoteEvent {
                event_id: format!("lake-quote-{day}-{step}"),
                timestamp: DateTime::from_micros((base + (32 * 60 * step) * 1_000_000_000) / 1_000),
                sequence: step as u64,
                symbol: "SPY".to_string(),
                bid_price: dec(&format!("{:.2}", mid - 0.01)),
                ask_price: dec(&format!("{:.2}", mid + 0.01)),
                bid_size: dec("100"),
                ask_size: dec("100"),
                venue: Some("XNYS".to_string()),
            }));
        }
        let trade = LakeEvent::Trade(TradeEvent {
            event_id: format!("lake-trade-{day}"),
            timestamp: DateTime::from_micros((base + 60 * 1_000_000_000) / 1_000),
            sequence: 12,
            symbol: "SPY".to_string(),
            price: dec("100.02"),
            quantity: dec("20"),
            venue: Some("XNYS".to_string()),
            trade_id: Some(format!("lake-trade-id-{day}")),
        });
        write_lake_partition(
            &lake,
            date,
            AssetClass::Equity,
            DataType::Quote,
            "SPY",
            &quotes,
        )
        .expect("write quote lake");
        write_lake_partition(
            &lake,
            date,
            AssetClass::Equity,
            DataType::Trade,
            "SPY",
            std::slice::from_ref(&trade),
        )
        .expect("write trade lake");
    }

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
        .arg("--data-dir")
        .arg(&lake)
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
    let _ = std::fs::remove_dir_all(&output_dir);
    let _ = std::fs::remove_dir_all(&lake);
}
