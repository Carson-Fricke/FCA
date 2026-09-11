use super::*;

fn counts(entries: &[(&str, u64)]) -> BTreeMap<String, u64> {
    entries
        .iter()
        .map(|(k, v)| ((*k).to_string(), *v))
        .collect()
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
        rows.iter()
            .find(|(key, _)| key == "run_id")
            .map(|(_, value)| value.as_str()),
        Some("run")
    );
    assert_eq!(
        rows.iter()
            .find(|(key, _)| key == "record_events")
            .map(|(_, value)| value.as_str()),
        Some("true")
    );
}
