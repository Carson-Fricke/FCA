use super::*;

#[test]
fn cargo_artifact_records_resolve_configured_target_and_ignore_dependencies() {
    let manifest = Path::new("/managed/crates/example/Cargo.toml");
    let record = |source: &str, kind: &str, files: serde_json::Value| {
        serde_json::json!({
        "reason": "compiler-artifact", "target": {"src_path": source, "crate_types": [kind]}, "filenames": files,
    }).to_string()
    };
    let dependency = record(
        "/sdk/src/lib.rs",
        "rlib",
        serde_json::json!(["/target/release/lib.rlib"]),
    );
    let authored = record(
        "/managed/crates/example/src/lib.rs",
        "cdylib",
        serde_json::json!([
            "/target/x86_64-pc-windows-msvc/release/program.dll",
            "/target/x86_64-pc-windows-msvc/release/program.pdb"
        ]),
    );
    assert_eq!(
        build_artifact(format!("{dependency}\n{authored}").as_bytes(), manifest),
        Some(PathBuf::from(
            "/target/x86_64-pc-windows-msvc/release/program.dll"
        ))
    );
    assert!(build_artifact(dependency.as_bytes(), manifest).is_none());
}

#[test]
fn cargo_json_only_classifies_authored_primary_errors() {
    let manifest = Path::new("/managed/crates/example/Cargo.toml");
    let record = |target: &str, primary: &str, spans: bool| {
        serde_json::json!({
            "reason": "compiler-message", "target": {"src_path": target},
            "message": {"level": "error", "rendered": "human compile detail\n", "spans": if spans {
                serde_json::json!([{"is_primary": true, "file_name": primary}])
            } else { serde_json::json!([]) }},
        })
        .to_string()
    };
    let authored = record("/managed/crates/example/src/lib.rs", "src/lib.rs", true);
    let (diagnostics, detail) = build_diagnostics(authored.as_bytes(), manifest);
    assert_eq!(diagnostics.len(), 1);
    assert!(detail.contains("human compile detail"));
    for infrastructure in [
        record("/sdk/src/lib.rs", "/sdk/src/lib.rs", true),
        record("/managed/crates/example/src/lib.rs", "", false),
    ] {
        assert!(build_diagnostics(infrastructure.as_bytes(), manifest)
            .0
            .is_empty());
        assert!(
            build_diagnostics(format!("{authored}\n{infrastructure}").as_bytes(), manifest)
                .0
                .is_empty()
        );
    }
    let windows = record("/managed/crates/example/src/lib.rs", "src\\lib.rs", true);
    assert_eq!(build_diagnostics(windows.as_bytes(), manifest).0.len(), 1);
    assert!(
        build_diagnostics(b"error: human text\n{broken json", manifest)
            .0
            .is_empty()
    );
}
