//! Cross-implementation job-result digest contract tests.
//!
//! The shared store and the engine manifest hash the same canonical payload
//! under the same job-based domain, so their digests must agree byte for byte.

use std::collections::BTreeMap;

use mcp::store::{artifact_digest, run_digest};

#[test]
fn store_and_manifest_run_digests_agree_on_the_job_vector() {
    let digests = BTreeMap::from([
        (
            "actions.parquet".to_string(),
            artifact_digest(b"actions-bytes"),
        ),
        ("fills.parquet".to_string(), artifact_digest(b"fills-bytes")),
    ]);
    let store_digest = run_digest("job-1", &digests);
    let manifest_digest = backtest::backtest::manifest::run_digest("job-1", &digests);
    assert_eq!(store_digest, manifest_digest);
    assert_eq!(
        store_digest,
        "19d6c08161185777e32626da04ad082e0730814d4c05fd61245065b88a1d044b"
    );
}

#[test]
fn run_digest_binds_the_job_identity_and_artifact_digests() {
    let digests = BTreeMap::from([(
        "actions.parquet".to_string(),
        artifact_digest(b"actions-bytes"),
    )]);
    let first = run_digest("job-1", &digests);
    assert_ne!(first, run_digest("job-2", &digests));
    let mut different = digests.clone();
    different.insert("fills.parquet".to_string(), artifact_digest(b"fills-bytes"));
    assert_ne!(first, run_digest("job-1", &different));
}
