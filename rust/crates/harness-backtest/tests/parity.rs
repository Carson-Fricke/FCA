//! Golden parity against the Python backtest pipeline.
//!
//! The goldens under `tests/golden/` are produced by the Python reference:
//!
//! * `synthetic_canonical_preflight.json` — the canonical `backtest.yaml`
//!   replay preflight evidence.
//! * `backtest_canonical.json` — the canonical `backtest.json` artifact.
//!
//! The preflight evidence is byte-identical today, which proves the synthetic
//! fixture generator, the replay merge order, and the domain-separated replay
//! fingerprint framing all match the Python `harness.data.replay` reference.
//!
//! The full `backtest.json` artifact is not yet byte-identical: the staged Rust
//! engine does not execute the configured `IntradayMomentumStrategy`, apply the
//! risk pipeline or fee schedule, or account the portfolio, so `counts`,
//! `dispatch_hash`, `fills`, `fees`, `portfolio`, and `risk` diverge. The
//! provenance fields `manifest.code_fingerprint` (Python source vs Rust source)
//! and `manifest.configuration_hash` (full pydantic model vs the reduced Rust
//! field set) also differ by construction.

use std::path::PathBuf;

use harness_backtest::backtest::fixture::{synthetic_fixture, SyntheticDataOptions};
use harness_backtest::backtest::job::{load_backtest_config, run_backtest_job};
use harness_backtest::backtest::preflight::preflight_data;
use harness_backtest::sha256::digest_hex;

fn crate_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn repo_root() -> PathBuf {
    crate_dir().join("..").join("..").join("..")
}

fn golden(name: &str) -> Vec<u8> {
    std::fs::read(crate_dir().join("tests").join("golden").join(name))
        .unwrap_or_else(|error| panic!("read golden {name}: {error}"))
}

fn canonical_config_path() -> PathBuf {
    repo_root()
        .join("config")
        .join("harness")
        .join("backtest.yaml")
}

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-backtest-parity-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

/// The first byte offset at which two payloads differ, plus both digests.
fn first_difference(expected: &[u8], actual: &[u8]) -> Option<String> {
    if expected == actual {
        return None;
    }
    let limit = expected.len().min(actual.len());
    let offset = (0..limit)
        .find(|index| expected[*index] != actual[*index])
        .unwrap_or(limit);
    let window = 24;
    let expected_excerpt = &expected[offset.min(expected.len())..];
    let actual_excerpt = &actual[offset.min(actual.len())..];
    let end = |payload: &[u8]| payload.len().min(window);
    Some(format!(
        "first difference at byte offset {offset}\n  golden {len_e} bytes sha256 {de}\n  actual {len_a} bytes sha256 {da}\n  golden[{offset}:] = {ge:?}\n  actual[{offset}:] = {ae:?}",
        len_e = expected.len(),
        de = digest_hex(expected),
        len_a = actual.len(),
        da = digest_hex(actual),
        ge = &expected_excerpt[..end(expected_excerpt)],
        ae = &actual_excerpt[..end(actual_excerpt)],
    ))
}

/// The canonical preflight evidence is byte-identical to the Python reference.
#[test]
fn canonical_preflight_is_byte_identical_to_python() {
    let configuration = load_backtest_config(&canonical_config_path()).expect("config");
    let fixture =
        synthetic_fixture(&configuration, &SyntheticDataOptions::default()).expect("fixture");
    let preflight = preflight_data(&configuration, &fixture).expect("preflight");
    let actual = preflight.to_json().to_canonical_string().into_bytes();
    let expected = golden("synthetic_canonical_preflight.json");
    assert!(
        first_difference(&expected, &actual).is_none(),
        "preflight diverged:\n{}",
        first_difference(&expected, &actual).unwrap_or_default()
    );
}

/// The full canonical `backtest.json` would be byte-identical to the Python
/// reference. It is ignored because the staged Rust engine does not yet execute
/// the configured strategy, risk pipeline, fees, or portfolio accounting; run
/// it explicitly to print the exact remaining divergence.
#[test]
#[ignore = "full backtest.json parity is blocked by un-ported strategy/risk/fee/portfolio execution and engine-specific provenance"]
fn canonical_artifact_is_byte_identical_to_python() {
    let directory = temp_dir("artifact");
    let summary =
        run_backtest_job(&canonical_config_path(), Some(&directory)).expect("job summary");
    let actual = std::fs::read(summary.artifact_path).expect("read artifact");
    let expected = golden("backtest_canonical.json");
    let report = first_difference(&expected, &actual);
    assert!(
        report.is_none(),
        "artifact diverged from the Python golden:\n{}",
        report.unwrap_or_default()
    );
    let _ = std::fs::remove_dir_all(&directory);
}
