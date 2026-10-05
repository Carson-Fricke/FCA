//! Compile/cache coordination for managed authored artifacts.

use std::path::{Path, PathBuf};

use backtest::sha256::digest_hex;

use crate::compiler::{run_build, sdk_dependency_path, write_authored_crate};
use crate::error::{artifact_missing, JitError};
use crate::provenance::compiler_fingerprint;
use crate::toolchain::resolve_toolchain;
use crate::workspace::{ManagedWorkspace, WorkspaceManifest};

pub use crate::validation::{validation_service, ValidationFailure, ValidationService};

/// The program kind an authored source exports.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProgramKind {
    /// A study program (`harness_sdk_study_vtable`).
    Study,
    /// A strategy program (`harness_sdk_strategy_vtable`).
    Strategy,
    /// A screening program (`harness_sdk_screening_vtable`).
    Screening,
}

impl ProgramKind {
    /// The lowercase manifest value.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Study => "study",
            Self::Strategy => "strategy",
            Self::Screening => "screening",
        }
    }

    /// The vtable export the kind must provide.
    #[must_use]
    pub const fn vtable_export(self) -> &'static str {
        match self {
            Self::Study => harness_strategy_sdk::STUDY_VTABLE_EXPORT,
            Self::Strategy => harness_strategy_sdk::STRATEGY_VTABLE_EXPORT,
            Self::Screening => harness_strategy_sdk::SCREENING_VTABLE_EXPORT,
        }
    }

    /// Parse a kind from its lowercase manifest value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "study" => Some(Self::Study),
            "strategy" => Some(Self::Strategy),
            "screening" => Some(Self::Screening),
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
    /// The absolute immutable artifact path.
    pub artifact_path: PathBuf,
    /// Whether the build was skipped in favor of the cached artifact.
    pub cached: bool,
    /// The resolved toolchain digest used for the build.
    pub toolchain_digest: String,
    /// The SDK crate version compiled into the artifact.
    pub sdk_version: String,
}

/// Compile or reuse one authored program, serialized per compiler/source across processes.
///
/// Cache reuse requires matching kind, compiler, SDK and ABI provenance and an existing
/// artifact. Returned artifacts are never replaced by subsequent builds.
///
/// # Errors
/// Returns [`JitError`] for workspace, compiler, or artifact failures.
pub fn compile(
    workspace: &ManagedWorkspace,
    source: &str,
    kind: ProgramKind,
) -> Result<CompiledArtifact, JitError> {
    compile_with_sdk(workspace, source, kind, Path::new(&sdk_dependency_path()))
}

fn compile_with_sdk(
    workspace: &ManagedWorkspace,
    source: &str,
    kind: ProgramKind,
    sdk: &Path,
) -> Result<CompiledArtifact, JitError> {
    if source.trim().is_empty() {
        return Err(JitError::Invalid("authored source is empty".to_string()));
    }
    let source = source.replace("\r\n", "\n");
    let digest = digest_hex(source.as_bytes());
    let toolchain = resolve_toolchain(workspace.root())?;
    let sdk_version = harness_strategy_sdk::SDK_VERSION.to_string();
    let sdk_abi_version = harness_strategy_sdk::SDK_ABI_VERSION;
    let sdk = std::path::absolute(sdk)?;
    let compiler_fingerprint = compiler_fingerprint(workspace.root(), &sdk, &toolchain.digest)?;
    let build_key =
        digest_hex(format!("{compiler_fingerprint}:{digest}:{}", kind.as_str()).as_bytes());
    let _compile_lock = workspace.lock(&build_key)?;

    let entries = workspace.read_manifest();
    if let Some(entry) = entries.get(&digest) {
        if entry.kind == kind.as_str()
            && entry.toolchain_digest == toolchain.digest
            && entry.compiler_fingerprint == compiler_fingerprint
            && entry.sdk_version == sdk_version
            && entry.sdk_abi_version == sdk_abi_version
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
    // Every attempt has its own crate and output directory, including legacy cache misses.
    // No caller can delete a crate Cargo is reading or replace a DLL another caller loads.
    let generation = workspace.create_build_directory()?;
    let crate_dir = workspace.crate_dir(&generation);
    let manifest = write_authored_crate(&crate_dir, &crate_name, &source, &sdk)?;
    let target_dir = workspace.target_dir().join(&generation);
    let artifact_path = run_build(workspace.root(), &manifest, &digest, &target_dir)?;
    let artifact_relpath = artifact_path
        .strip_prefix(workspace.target_dir())
        .map_err(|error| JitError::Io(format!("Cargo artifact outside managed target: {error}")))?;
    if !artifact_path.is_file() {
        return Err(artifact_missing(&digest, &artifact_path));
    }

    workspace.update_manifest(
        digest.clone(),
        WorkspaceManifest {
            kind: kind.as_str().to_string(),
            crate_name: crate_name.clone(),
            artifact_relpath: artifact_relpath.to_string_lossy().into_owned(),
            toolchain_digest: toolchain.digest.clone(),
            compiler_fingerprint,
            sdk_version: sdk_version.clone(),
            sdk_abi_version,
            built_at_unix: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |value| value.as_secs()),
        },
    )?;
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

#[cfg(test)]
#[path = "../tests/unit/fixture.rs"]
pub(crate) mod fixture;

#[cfg(test)]
#[path = "../tests/unit/compile.rs"]
mod tests;
