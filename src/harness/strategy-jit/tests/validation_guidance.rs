use harness_strategy_jit::JitError;
use harness_strategy_jit::ValidationFailure;
use serde_json::json;

#[test]
fn validation_compile_message_is_compact_and_points_to_structured_errors() {
    let diagnostics =
        vec![json!({"message": "mismatched types", "level": "error", "code": {"code": "E0308"}})];
    let failure = ValidationFailure::from(JitError::Compile {
        digest: "abc".into(),
        detail: "Compiling example\nrendered error".repeat(10000),
        diagnostics: diagnostics.clone(),
    });
    assert!(failure.message.len() < 250);
    assert!(failure.message.contains("validate_study again"));
    assert!(!failure.message.contains("Compiling"));
    assert_eq!(failure.diagnostics, diagnostics);
    assert!(failure.cacheable);
    assert!(failure.deterministic_compiler_diagnostic);
}

#[test]
fn missing_abi_export_includes_current_study_template_not_loader_noise() {
    let failure = ValidationFailure::from(JitError::Export {
        symbol: "harness_sdk_abi_version".into(),
        detail: "OS loader noise".repeat(1000),
    });
    for guidance in [
        "harness_sdk_abi_version",
        "export_study_program!(MyStudy);",
        "impl harness_strategy_sdk::MarketConsumer",
        "type Context = harness_strategy_sdk::ObservationContext;",
        "fn subscriptions(&self)",
        "impl harness_strategy_sdk::StudyProgram",
        "harness_strategy_sdk::serde_json::Value",
        "harnessConfiguration",
        "validate_study again",
    ] {
        assert!(failure.message.contains(guidance), "missing {guidance}");
    }
    assert!(!failure.message.contains("OS loader noise"));
    assert!(failure.diagnostics.is_empty());
    assert!(!failure.deterministic_compiler_diagnostic);
    assert!(!failure.cacheable);
}

#[test]
fn infrastructure_compile_failure_stays_distinct_and_uncacheable() {
    let failure = ValidationFailure::from(JitError::Compile {
        digest: "abc".into(),
        detail: "error: linker not found".into(),
        diagnostics: vec![],
    });
    assert!(failure.message.contains("linker not found"));
    assert!(!failure.deterministic_compiler_diagnostic);
    assert!(!failure.cacheable);
}
