//! Generated crate construction and Cargo invocation.

use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use crate::diagnostics::build_artifact;
use crate::diagnostics::build_diagnostics;
use crate::diagnostics::output_tail;
use crate::error::JitError;

pub(super) fn sdk_dependency_path() -> String {
    let base = std::env::var("HARNESS_SDK_DIR")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map_or_else(
            || Path::new(env!("CARGO_MANIFEST_DIR")).join("../strategy-sdk"),
            PathBuf::from,
        );
    base.to_string_lossy().replace('\\', "/")
}

pub(super) fn write_authored_crate(
    crate_dir: &Path,
    crate_name: &str,
    source: &str,
    sdk: &Path,
) -> Result<PathBuf, JitError> {
    let source_dir = crate_dir.join("src");
    std::fs::create_dir_all(&source_dir)?;
    let manifest = format!(
        "# Authored harness program, content-addressed by the JIT seam.\n\
         [package]\n\
         name = \"{crate_name}\"\n\
         version = \"0.0.0\"\n\
         edition = \"2021\"\n\
         publish = false\n\n\
         [workspace]\n\n\
         [lib]\n\
         crate-type = [\"cdylib\"]\n\n\
         [dependencies]\n\
         harness-strategy-sdk = {{ path = {} }}\n",
        serde_json::to_string(&sdk.to_string_lossy().replace('\\', "/"))
            .map_err(|error| JitError::Io(error.to_string()))?
    );
    let manifest_path = crate_dir.join("Cargo.toml");
    std::fs::write(&manifest_path, manifest)?;
    std::fs::write(source_dir.join("lib.rs"), source)?;
    Ok(manifest_path)
}

pub(super) fn run_build(
    workspace: &Path,
    manifest: &Path,
    digest: &str,
    target_dir: &Path,
) -> Result<PathBuf, JitError> {
    let output = Command::new("cargo")
        .args([
            "build",
            "--release",
            "--message-format=json",
            "--manifest-path",
        ])
        .arg(manifest)
        .arg("--target-dir")
        .arg(target_dir)
        .current_dir(workspace)
        .env_remove("RUSTC_WRAPPER")
        .env_remove("RUSTC_WORKSPACE_WRAPPER")
        .output()
        .map_err(|error| JitError::Io(format!("running cargo build: {error}")))?;
    if !output.status.success() {
        let (diagnostics, rendered) = build_diagnostics(&output.stdout, manifest);
        return Err(JitError::Compile {
            digest: digest.to_string(),
            detail: output_tail(
                format!("{}\n{}", rendered, String::from_utf8_lossy(&output.stderr)).as_bytes(),
            ),
            diagnostics,
        });
    }
    build_artifact(&output.stdout, manifest)
        .ok_or_else(|| crate::error::artifact_missing(digest, target_dir))
}
