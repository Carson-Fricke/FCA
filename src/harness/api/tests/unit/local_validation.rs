//! Full local validation result reuse and structured diagnostic boundary.

use crate::local::NativeStudyValidator;
use mcp::tools::StudyValidator;
use serde_json::json;

#[test]
fn local_validator_reuses_full_success_and_deterministic_failure_results() {
    let model = json!({
        "source": include_str!("../../../strategy-jit/tests/fixtures/study_program.rs"),
        "harnessConfiguration": {
            "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
            "interval": "1min", "symbols": ["AAA", "BBB", "CCC"],
        },
    });
    let first = NativeStudyValidator.validate_result(&model).unwrap();
    assert_eq!(first["valid"], true);
    assert_eq!(first["reused"], false);
    assert_eq!(first["cacheable"], true);
    assert!(first["validationIdentity"]
        .as_str()
        .unwrap()
        .starts_with("sha256:"));
    let second = NativeStudyValidator.validate_result(&model).unwrap();
    assert_eq!(second["reused"], true);
    assert_eq!(first["subscriptions"], second["subscriptions"]);

    let mut changed = model.clone();
    changed["harnessConfiguration"]["interval"] = json!("1d");
    let mismatch = NativeStudyValidator.validate_result(&changed).unwrap_err();
    assert!(mismatch.message().contains("do not match"));
    assert_eq!(
        mismatch.payload().unwrap()["deterministicCompilerDiagnostic"],
        false
    );
    assert_eq!(mismatch.payload().unwrap()["reused"], false);
    assert_eq!(
        NativeStudyValidator
            .validate_result(&changed)
            .unwrap_err()
            .payload()
            .unwrap()["reused"],
        true
    );

    changed["source"] = json!("this is not rust");
    let failure = NativeStudyValidator.validate_result(&changed).unwrap_err();
    let payload = failure.payload().unwrap();
    assert_eq!(payload["reused"], false);
    assert_eq!(payload["deterministicCompilerDiagnostic"], true);
    assert!(!payload["diagnostics"].as_array().unwrap().is_empty());
    assert!(failure.message().contains("error"));
    let reused = NativeStudyValidator.validate_result(&changed).unwrap_err();
    assert_eq!(reused.payload().unwrap()["reused"], true);
    assert_eq!(
        reused.payload().unwrap()["diagnostics"],
        payload["diagnostics"]
    );
}
