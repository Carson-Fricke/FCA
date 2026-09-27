//! Authoring-source compilation into managed cdylib artifacts.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use backtest::sha256::digest_hex;

use crate::error::{artifact_missing, JitError};
use crate::toolchain::resolve_toolchain;
use crate::workspace::{ManagedWorkspace, WorkspaceManifest};

/// The environment variable overriding the SDK source directory at run time.
///
/// Serving images copy the `strategy-sdk` and `num` crate sources to a known
/// location (as siblings, so the SDK's `../num` path dependency resolves) and
/// point `HARNESS_SDK_DIR` at the SDK; without it the build-time path of this
/// crate is used.
const SDK_DIR_VARIABLE: &str = "HARNESS_SDK_DIR";

/// The SDK dependency path written into the authored manifest, with the
/// separators normalized so TOML never reads a Windows backslash as an
/// escape.
fn sdk_dependency_path() -> String {
    let base = std::env::var(SDK_DIR_VARIABLE)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map_or_else(
            || {
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("..")
                    .join("strategy-sdk")
            },
            PathBuf::from,
        );
    base.to_string_lossy().replace('\\', "/")
}

/// The tail of captured build output kept in compile failures.
const OUTPUT_TAIL_BYTES: usize = 4000;

/// The program kind an authored source exports.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProgramKind {
    /// A study program (`harness_sdk_study_vtable`).
    Study,
    /// A strategy program (`harness_sdk_strategy_vtable`).
    Strategy,
}

impl ProgramKind {
    /// The lowercase manifest value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Study => "study",
            Self::Strategy => "strategy",
        }
    }

    /// The vtable export the kind must provide.
    #[must_use]
    pub const fn vtable_export(self) -> &'static str {
        match self {
            Self::Study => harness_strategy_sdk::STUDY_VTABLE_EXPORT,
            Self::Strategy => harness_strategy_sdk::STRATEGY_VTABLE_EXPORT,
        }
    }

    /// Parse a kind from its lowercase manifest value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "study" => Some(Self::Study),
            "strategy" => Some(Self::Strategy),
            _ => None,
        }
    }
}

/// One compiled authored program.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompiledArtifact {
    /// The SHA-256 digest of the authored source.
    pub digest: String,
    /// The program kind.
    pub kind: ProgramKind,
    /// The authored crate package name.
    pub crate_name: String,
    /// The absolute artifact path.
    pub artifact_path: PathBuf,
    /// Whether the build was skipped in favor of the cached artifact.
    pub cached: bool,
    /// The resolved toolchain digest used for the build.
    pub toolchain_digest: String,
    /// The SDK crate version compiled into the artifact.
    pub sdk_version: String,
}

/// Compile (or fetch from cache) one authored program source.
///
/// The digest is the SHA-256 of the source; a cached artifact is reused when
/// the recorded kind, toolchain digest, and SDK version all match and the
/// artifact file still exists.
///
/// # Errors
///
/// Returns [`JitError`] when the workspace cannot be prepared, the build
/// fails, or the artifact cannot be located.
pub fn compile(
    workspace: &ManagedWorkspace,
    source: &str,
    kind: ProgramKind,
) -> Result<CompiledArtifact, JitError> {
    if source.trim().is_empty() {
        return Err(JitError::Invalid("authored source is empty".to_string()));
    }
    // Normalize CRLF so the cache digest is checkout-independent, matching
    // the bundle-hash provenance digest.
    let normalized = source.replace("\r\n", "\n");
    let digest = digest_hex(normalized.as_bytes());
    let source = normalized;
    let toolchain = resolve_toolchain(workspace.root())?;
    let sdk_version = harness_strategy_sdk::SDK_VERSION.to_string();

    let entries = workspace.read_manifest();
    if let Some(entry) = entries.get(&digest) {
        if entry.kind == kind.as_str()
            && entry.toolchain_digest == toolchain.digest
            && entry.sdk_version == sdk_version
        {
            let artifact = workspace.target_dir().join(&entry.artifact_relpath);
            if artifact.is_file() {
                return Ok(CompiledArtifact {
                    digest,
                    kind,
                    crate_name: entry.crate_name.clone(),
                    artifact_path: artifact,
                    cached: true,
                    toolchain_digest: toolchain.digest,
                    sdk_version,
                });
            }
        }
    }

    let crate_name = format!("harness_authored_{}", &digest[..16]);
    write_authored_crate(workspace, &digest, &crate_name, &source)?;
    run_build(workspace, &digest)?;
    let artifact_relpath = artifact_relpath(&crate_name);
    let artifact_path = workspace.target_dir().join(&artifact_relpath);
    if !artifact_path.is_file() {
        return Err(artifact_missing(&digest, &artifact_path));
    }

    let mut entries: BTreeMap<String, WorkspaceManifest> = workspace.read_manifest();
    entries.insert(
        digest.clone(),
        WorkspaceManifest {
            kind: kind.as_str().to_string(),
            crate_name: crate_name.clone(),
            artifact_relpath: artifact_relpath.clone(),
            toolchain_digest: toolchain.digest.clone(),
            sdk_version: sdk_version.clone(),
            built_at_unix: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |value| value.as_secs()),
        },
    );
    workspace.write_manifest(&entries)?;

    Ok(CompiledArtifact {
        digest,
        kind,
        crate_name,
        artifact_path,
        cached: false,
        toolchain_digest: toolchain.digest,
        sdk_version,
    })
}

fn write_authored_crate(
    workspace: &ManagedWorkspace,
    digest: &str,
    crate_name: &str,
    source: &str,
) -> Result<PathBuf, JitError> {
    let crate_dir = workspace.crate_dir(digest);
    if crate_dir.exists() {
        std::fs::remove_dir_all(&crate_dir)
            .map_err(|error| JitError::Io(format!("clearing {}: {error}", crate_dir.display())))?;
    }
    let source_dir = crate_dir.join("src");
    std::fs::create_dir_all(&source_dir)
        .map_err(|error| JitError::Io(format!("creating {}: {error}", source_dir.display())))?;
    let manifest = format!(
        "# Authored harness program, content-addressed by the JIT seam.\n\
         [package]\n\
         name = \"{crate_name}\"\n\
         version = \"0.0.0\"\n\
         edition = \"2021\"\n\
         publish = false\n\
         \n\
         [lib]\n\
         crate-type = [\"cdylib\"]\n\
         \n\
         [dependencies]\n\
         harness-strategy-sdk = {{ path = \"{}\" }}\n",
        sdk_dependency_path()
    );
    std::fs::write(crate_dir.join("Cargo.toml"), manifest.as_bytes())
        .map_err(|error| JitError::Io(format!("writing the authored manifest: {error}")))?;
    std::fs::write(source_dir.join("lib.rs"), source.as_bytes())
        .map_err(|error| JitError::Io(format!("writing the authored source: {error}")))?;
    Ok(crate_dir.join("Cargo.toml"))
}

fn run_build(workspace: &ManagedWorkspace, digest: &str) -> Result<(), JitError> {
    let manifest_path = workspace.crate_dir(digest).join("Cargo.toml");
    let mut command = Command::new("cargo");
    command
        .arg("build")
        .arg("--release")
        .arg("--manifest-path")
        .arg(&manifest_path)
        .arg("--target-dir")
        .arg(workspace.target_dir())
        .current_dir(workspace.root())
        .env_remove("RUSTC_WRAPPER")
        .env_remove("RUSTC_WORKSPACE_WRAPPER");
    let output = command
        .output()
        .map_err(|error| JitError::Io(format!("running cargo build: {error}")))?;
    if !output.status.success() {
        return Err(JitError::Compile {
            digest: digest.to_string(),
            detail: output_tail(&output.stderr),
        });
    }
    Ok(())
}

fn output_tail(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    let tail: String = text
        .chars()
        .rev()
        .take(OUTPUT_TAIL_BYTES)
        .collect::<Vec<char>>()
        .into_iter()
        .rev()
        .collect();
    tail.trim().to_string()
}

fn artifact_relpath(crate_name: &str) -> String {
    let file = if cfg!(target_os = "windows") {
        format!("{crate_name}.dll")
    } else if cfg!(target_os = "macos") {
        format!("lib{crate_name}.dylib")
    } else {
        format!("lib{crate_name}.so")
    };
    Path::new("release").join(file).display().to_string()
}
