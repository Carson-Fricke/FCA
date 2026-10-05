use super::*;
use serde_json::json;

#[test]
fn compiler_errors_keep_locations_and_fixes_without_rust_reference_or_duplicates() {
    let manifest = Path::new("/managed/example/Cargo.toml");
    let message = json!({
        "reason": "compiler-message", "target": {"src_path": "/managed/example/src/lib.rs"},
        "message": {
            "message": "mismatched types", "level": "error",
            "code": {"code": "E0308", "explanation": "giant Rust reference".repeat(10000)},
            "rendered": "error[E0308]: mismatched types\n",
            "spans": [{"file_name": "src/lib.rs", "is_primary": true,
                "line_start": 12, "line_end": 12, "column_start": 3, "column_end": 8,
                "label": "expected f64", "text": [{"text": "huge source".repeat(1000)}],
                "expansion": {"span": {"text": "macro expansion".repeat(1000)}}}],
            "children": [{"message": "convert the value", "level": "help", "rendered": "duplicate",
                "spans": [{"file_name": "src/lib.rs", "line_start": 12, "column_start": 3,
                    "suggested_replacement": "value.to_string().parse::<f64>()?",
                    "suggestion_applicability": "MachineApplicable"}], "children": []}]
        }
    });
    let warning = json!({"reason": "compiler-message", "target": {"src_path": "/managed/example/src/lib.rs"},
        "message": {"level": "warning", "rendered": "unused variable noise", "spans": []}});
    let (diagnostics, rendered) =
        build_diagnostics(format!("{warning}\n{message}").as_bytes(), manifest);
    assert_eq!(diagnostics.len(), 1);
    let diagnostic = &diagnostics[0];
    assert_eq!(diagnostic["message"], "mismatched types");
    assert_eq!(diagnostic["code"], json!({"code": "E0308"}));
    assert_eq!(diagnostic["spans"][0]["line_start"], 12);
    assert_eq!(
        diagnostic["children"][0]["spans"][0]["suggestion_applicability"],
        "MachineApplicable"
    );
    assert!(diagnostic.to_string().len() < 1000);
    assert!(!diagnostic.to_string().contains("explanation"));
    assert!(!diagnostic.to_string().contains("rendered"));
    assert_eq!(rendered, "error[E0308]: mismatched types\n");
}

#[test]
fn output_tail_removes_build_progress_but_keeps_infrastructure_failures() {
    let output = b"   Updating crates.io index\n     Locking 3 packages\n      Adding serde v1\n Downloading crates ...\n  Downloaded serde v1\n   Compiling example v0.1\n    Checking sdk v0.1\n    Blocking waiting for file lock\nerror: linker not found\nCaused by:\n  executable missing\n    Finished release target\n";
    assert_eq!(
        output_tail(output),
        "error: linker not found\nCaused by:\n  executable missing"
    );
    assert!(output_tail("x".repeat(5000).as_bytes()).len() <= 4000);
}
