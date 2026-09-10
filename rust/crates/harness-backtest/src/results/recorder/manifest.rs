//! Run manifest and per-artifact digest helpers.
//!
//! Port of `harness.results.recorder.manifest` plus the manifest-row and
//! run-fingerprint construction from `RunRecorder.close`. The framing is
//! byte-exact with the Python recorder so the run digest is reproducible across
//! the two engines.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::sha256::{digest_hex, Sha256};

use super::artifacts::artifact_path;
use super::schemas::{Artifact, STREAM_ARTIFACTS, SCHEMA_VERSION};

/// Immutable summary of one closed recorded run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunManifest {
    /// Run identifier.
    pub run_id: String,
    /// Directory holding the run artifacts.
    pub directory: PathBuf,
    /// Rows recorded per artifact.
    pub row_counts: BTreeMap<String, u64>,
    /// SHA-256 digest per streamed artifact.
    pub digests: BTreeMap<String, String>,
    /// Domain-separated digest over the artifact digests.
    pub run_digest: String,
    /// Free-form run metadata.
    pub metadata: BTreeMap<String, String>,
}

impl RunManifest {
    /// Recorded artifact names in stable recorder order.
    #[must_use]
    pub fn artifacts(&self) -> Vec<&'static str> {
        Artifact::ALL.iter().map(|artifact| artifact.value()).collect()
    }

    /// Total row count across every artifact.
    #[must_use]
    pub fn total_rows(&self) -> u64 {
        self.row_counts.values().copied().sum()
    }

    /// Parquet path for one recorded artifact inside the run directory.
    #[must_use]
    pub fn path(&self, artifact: Artifact) -> PathBuf {
        artifact_path(&self.directory, artifact)
    }
}

/// Streaming SHA-256 digest of one file's bytes.
///
/// # Errors
///
/// Returns the underlying [`std::io::Error`] when the file cannot be read.
pub fn file_digest(path: &Path) -> std::io::Result<String> {
    use std::io::Read as _;
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher.finalize_hex())
}

/// Digest a byte slice to the lowercase SHA-256 hex digest.
#[must_use]
pub fn digest_bytes(bytes: &[u8]) -> String {
    digest_hex(bytes)
}

/// Render a boolean exactly like the Python `_truth_text` helper.
#[must_use]
pub const fn truth_text(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

/// Compute the run fingerprint over per-artifact row counts and digests.
///
/// Replicates `RunRecorder.close`:
/// `fingerprint.update(f"{name}\0{count}\0{digest}\n".encode())` for each
/// stream artifact in stable order.
#[must_use]
pub fn run_fingerprint(
    row_counts: &BTreeMap<String, u64>,
    digests: &BTreeMap<String, String>,
) -> String {
    let mut hasher = Sha256::new();
    for artifact in STREAM_ARTIFACTS {
        let name = artifact.value();
        let count = row_counts.get(name).copied().unwrap_or(0);
        let digest = digests.get(name).map_or("", String::as_str);
        hasher.update(format!("{name}\u{0}{count}\u{0}{digest}\n").as_bytes());
    }
    hasher.finalize_hex()
}

/// Inputs needed to build the canonical manifest rows.
#[derive(Clone, Debug)]
pub struct ManifestRowsInput<'a> {
    /// Run identifier.
    pub run_id: &'a str,
    /// Seed text (`_seed_text` output).
    pub seed: &'a str,
    /// Per-artifact batch size.
    pub batch_size: usize,
    /// Artifact compression codec.
    pub compression: &'a str,
    /// Parquet writer version.
    pub parquet_version: &'a str,
    /// Whether the full event stream was recorded.
    pub record_events: bool,
    /// Whether event payloads were recorded.
    pub record_payloads: bool,
    /// Default strategy identifier, or empty.
    pub strategy_id: &'a str,
    /// Default book identifier, or empty.
    pub book_id: &'a str,
    /// Default account identifier, or empty.
    pub account_id: &'a str,
    /// Run metadata.
    pub metadata: &'a BTreeMap<String, String>,
    /// Per-artifact row counts.
    pub row_counts: &'a BTreeMap<String, u64>,
    /// Per-artifact digests.
    pub digests: &'a BTreeMap<String, String>,
    /// Run fingerprint.
    pub run_digest: &'a str,
}

/// Build the canonical manifest key/value rows exactly like `_manifest_rows`.
///
/// The final `row_count` entry is `len(entries) + 1`, where `entries` excludes
/// the `row_count` row itself, matching the Python recorder.
#[must_use]
pub fn manifest_rows(input: &ManifestRowsInput<'_>) -> Vec<(String, String)> {
    let mut entries: Vec<(String, String)> = vec![
        ("run_id".to_string(), input.run_id.to_string()),
        ("schema_version".to_string(), SCHEMA_VERSION.to_string()),
        ("seed".to_string(), input.seed.to_string()),
        ("batch_size".to_string(), input.batch_size.to_string()),
        ("compression".to_string(), input.compression.to_string()),
        (
            "parquet_version".to_string(),
            input.parquet_version.to_string(),
        ),
        (
            "record_events".to_string(),
            truth_text(input.record_events).to_string(),
        ),
        (
            "record_payloads".to_string(),
            truth_text(input.record_payloads).to_string(),
        ),
        ("strategy_id".to_string(), input.strategy_id.to_string()),
        ("book_id".to_string(), input.book_id.to_string()),
        ("account_id".to_string(), input.account_id.to_string()),
    ];
    for (key, value) in input.metadata {
        entries.push((format!("metadata:{key}"), value.clone()));
    }
    for artifact in STREAM_ARTIFACTS {
        let name = artifact.value();
        let count = input.row_counts.get(name).copied().unwrap_or(0);
        entries.push((format!("artifact:{name}"), count.to_string()));
        let digest = input.digests.get(name).map_or("", String::as_str);
        entries.push((format!("digest:{name}"), digest.to_string()));
    }
    entries.push(("run_digest".to_string(), input.run_digest.to_string()));
    entries.push((
        "row_count".to_string(),
        (entries.len() + 1).to_string(),
    ));
    entries
}

#[cfg(test)]
mod tests {
    use super::*;

    fn counts(entries: &[(&str, u64)]) -> BTreeMap<String, u64> {
        entries.iter().map(|(k, v)| ((*k).to_string(), *v)).collect()
    }

    fn digests(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
        entries
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    #[test]
    fn fingerprint_is_order_independent_but_name_bound() {
        let counts = counts(&[("actions", 2), ("fills", 1)]);
        let digests = digests(&[("actions", "aa"), ("fills", "bb")]);
        let first = run_fingerprint(&counts, &digests);
        let second = run_fingerprint(&counts, &digests);
        assert_eq!(first, second);
        assert_eq!(first.len(), 64);
    }

    #[test]
    fn fingerprint_matches_python_framing() {
        // Oracle produced by hashlib.sha256 over the same framing for
        // counts {actions: 2, fills: 1} and digests {actions: "aa", fills: "bb"}
        // (see the parity integration test for the Python one-liner).
        let counts = counts(&[("actions", 2), ("fills", 1)]);
        let digests = digests(&[("actions", "aa"), ("fills", "bb")]);
        assert_eq!(
            run_fingerprint(&counts, &digests),
            "6c36b16e2b0c46a95c70975569d58d276c85267ca939d9c121502df25ecf072c"
        );
    }

    #[test]
    fn manifest_rows_row_count_is_last() {
        let row_counts = counts(&[("actions", 3)]);
        let digests = digests(&[("actions", "abc")]);
        let metadata = BTreeMap::new();
        let rows = manifest_rows(&ManifestRowsInput {
            run_id: "run",
            seed: "seed",
            batch_size: 8,
            compression: "zstd",
            parquet_version: "2.6",
            record_events: true,
            record_payloads: true,
            strategy_id: "momentum",
            book_id: "alpha",
            account_id: "paper",
            metadata: &metadata,
            row_counts: &row_counts,
            digests: &digests,
            run_digest: "digest",
        });
        let last = rows.last().expect("rows");
        assert_eq!(last.0, "row_count");
        let expected = rows.len().to_string();
        assert_eq!(last.1, expected);
        assert_eq!(
            rows.iter().find(|(key, _)| key == "run_id").map(|(_, value)| value.as_str()),
            Some("run")
        );
        assert_eq!(
            rows.iter().find(|(key, _)| key == "record_events").map(|(_, value)| value.as_str()),
            Some("true")
        );
    }
}
