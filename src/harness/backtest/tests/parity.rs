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
//! The full `backtest.json` artifact now runs on the native Rust engine core
//! (clock, event bus, replay, venue, fees, portfolio, risk) driving the real
//! Python `IntradayMomentumStrategy` through the harness-host PyO3 adapter.
//! Parity is byte-for-byte across every data-bearing artifact — `counts`,
//! `dispatch_hash`, `fills`, `fees`, `portfolio`, `risk`, and the manifest's
//! `configuration_hash` / `fill_model_fingerprint` / `fee_fingerprint` /
//! `risk_pipeline_fingerprint`.
//!
//! Two provenance differences are expected and excluded by the agreed parity
//! definition:
//!
//! * `manifest.code_fingerprint` hashes the engine's own source. Python hashes
//!   the Python engine modules (`57215b5e…`); Rust hashes the Rust engine
//!   modules. The two differ by construction, so the comparison masks the 64
//!   hex digits of that one field in both payloads.
//! * The golden in this checkout carries CRLF line endings while the writer
//!   emits LF; both payloads are newline-normalized before comparison.

use std::path::PathBuf;

use backtest::backtest::fixture::{synthetic_fixture, SyntheticDataOptions};
use backtest::backtest::job::load_backtest_config;
use backtest::backtest::preflight::preflight_data;
use backtest::sha256::digest_hex;

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

/// Mask the 64 hex digits of `manifest.code_fingerprint` and normalize newlines.
fn mask_code_fingerprint(bytes: &[u8]) -> Vec<u8> {
    let text = String::from_utf8_lossy(bytes).replace("\r\n", "\n");
    let trimmed = text.trim_end_matches('\n');
    let marker = "\"code_fingerprint\":\"";
    if let Some(start) = trimmed.find(marker) {
        let value_start = start + marker.len();
        let mut out = trimmed.as_bytes().to_vec();
        if value_start + 64 <= out.len() {
            for byte in &mut out[value_start..value_start + 64] {
                *byte = b'x';
            }
        }
        out
    } else {
        trimmed.as_bytes().to_vec()
    }
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

/// The native engine artifact is byte-identical to the Python reference under
/// the agreed parity definition.
#[test]
fn canonical_artifact_matches_python_except_code_fingerprint() {
    let directory = temp_dir("artifact");
    let (outcome, artifact_path) =
        host::backtest::run_backtest_job(&canonical_config_path(), Some(&directory))
            .expect("native job");
    assert!(outcome.completed());
    assert_eq!(outcome.dispatched_count, 330);
    assert_eq!(outcome.order_count, 26);
    assert_eq!(outcome.fills.len(), 26);
    assert_eq!(outcome.risk_decisions.len(), 26);
    // The Rust engine reports its own stable code fingerprint; it differs from
    // the Python reference by construction.
    assert_eq!(
        outcome.manifest.code_fingerprint,
        backtest::backtest::manifest::code_fingerprint()
    );

    let actual = std::fs::read(&artifact_path).expect("read artifact");
    let expected = golden("backtest_canonical.json");
    let masked_expected = mask_code_fingerprint(&expected);
    let masked_actual = mask_code_fingerprint(&actual);
    let report = first_difference(&masked_expected, &masked_actual);
    assert!(
        report.is_none(),
        "artifact diverged from the Python golden beyond code_fingerprint:\n{}",
        report.unwrap_or_default()
    );
    let _ = std::fs::remove_dir_all(&directory);
}
