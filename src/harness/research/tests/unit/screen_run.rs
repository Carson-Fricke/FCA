use super::*;
use backtest::backtest::screen::ScreenConfig;

const DEFINITION_ID: &str =
    "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const SOURCE_DIGEST: &str = "2222222222222222222222222222222222222222222222222222222222222222";
const TOOLCHAIN_DIGEST: &str = "3333333333333333333333333333333333333333333333333333333333333333";

fn configuration(job_id: &str) -> ScreenConfig {
    let source = "// screening source\n";
    ScreenConfig {
        schema_version: 1,
        shell: "screen".to_string(),
        job_id: job_id.to_string(),
        name: "momentum-screen".to_string(),
        as_of: "2024-01-05".to_string(),
        bar_intervals: vec!["1d".to_string()],
        lookback_days: 20,
        rank_metric: "momentum".to_string(),
        universe_revision: None,
        source: source.to_string(),
        source_sha256: digest_hex(source.as_bytes()),
        bundle_hash: digest_hex(source.as_bytes()),
        definition_id: DEFINITION_ID.to_string(),
        definition_hash: "4444444444444444444444444444444444444444444444444444444444444444"
            .to_string(),
        parameters: serde_json::json!({}),
    }
}

#[test]
fn screen_artifact_contract_is_v2_with_the_job_vocabulary() {
    assert_eq!(SCREEN_ARTIFACT, "screen.json");
    assert_eq!(SCREEN_SCHEMA, "fca-harness-screen-v2");
    assert_eq!(ScreenStatus::Succeeded.value(), "succeeded");
    assert_eq!(ScreenStatus::Failed.value(), "failed");
}

#[test]
fn artifact_document_carries_the_v2_schema_and_job_identity() {
    let configuration = configuration("job-screen-1");
    let document = artifact_document(
        &configuration,
        "job-screen-1",
        ScreenStatus::Succeeded,
        None,
        &[],
        SOURCE_DIGEST,
        TOOLCHAIN_DIGEST,
        "screen-digest",
    )
    .expect("artifact document");
    assert_eq!(document["schema"], "fca-harness-screen-v2");
    assert_eq!(document["status"], "succeeded");
    assert_eq!(document["job_id"], "job-screen-1");
    assert_eq!(document["provenance"]["run_digest"], "screen-digest");

    let failed = artifact_document(
        &configuration,
        "job-screen-1",
        ScreenStatus::Failed,
        Some("the program failed"),
        &[],
        SOURCE_DIGEST,
        TOOLCHAIN_DIGEST,
        "screen-digest",
    )
    .expect("failed artifact document");
    assert_eq!(failed["schema"], "fca-harness-screen-v2");
    assert_eq!(failed["status"], "failed");
    assert_eq!(failed["error"], "the program failed");
}

#[test]
fn screen_job_digest_vector_is_job_based() {
    let configuration = configuration("job-screen-1");
    let digest = run_digest(
        "job-screen-1",
        SOURCE_DIGEST,
        TOOLCHAIN_DIGEST,
        &configuration,
        ScreenStatus::Succeeded,
    );
    assert_eq!(
        digest,
        "8e7acc40c0311ea455bce6ea9882ca73aed03970624c56d6c9027b1469548a0e"
    );
    assert_ne!(
        digest,
        run_digest(
            "job-screen-1",
            SOURCE_DIGEST,
            TOOLCHAIN_DIGEST,
            &configuration,
            ScreenStatus::Failed,
        )
    );
}
