//! The managed cargo workspace that holds authored crates and their builds.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::Value as Json;

use crate::error::JitError;

/// The toolchain pin copied into every managed workspace.
const TOOLCHAIN_FILE: &str = r#"# Managed harness JIT workspace: toolchain pinned to the harness channel.
[toolchain]
channel = "stable"
components = ["rustfmt", "clippy"]
profile = "minimal"
"#;

/// The managed-workspace manifest file name.
pub const MANIFEST_FILE: &str = "manifest.json";

/// One compiled authored program recorded in the workspace manifest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkspaceManifest {
    /// The program kind (`study` or `strategy`).
    pub kind: String,
    /// The authored crate package name.
    pub crate_name: String,
    /// The artifact path relative to the workspace target directory.
    pub artifact_relpath: String,
    /// The resolved toolchain digest used for the build.
    pub toolchain_digest: String,
    /// The SDK crate version compiled into the artifact.
    pub sdk_version: String,
    /// The build completion time as seconds since the Unix epoch.
    pub built_at_unix: u64,
}

/// A managed cargo workspace with one shared `target/` and per-source crates.
pub struct ManagedWorkspace {
    root: PathBuf,
}

impl ManagedWorkspace {
    /// Open (or create) the managed workspace at `root`.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when the directories cannot be created or the
    /// toolchain pin cannot be written.
    pub fn open(root: &Path) -> Result<Self, JitError> {
        std::fs::create_dir_all(root.join("crates")).map_err(|error| {
            JitError::Io(format!(
                "creating {}: {error}",
                root.join("crates").display()
            ))
        })?;
        std::fs::create_dir_all(root.join("target")).map_err(|error| {
            JitError::Io(format!(
                "creating {}: {error}",
                root.join("target").display()
            ))
        })?;
        std::fs::write(root.join("rust-toolchain.toml"), TOOLCHAIN_FILE)
            .map_err(|error| JitError::Io(format!("writing the toolchain pin: {error}")))?;
        Ok(Self {
            root: root.to_path_buf(),
        })
    }

    /// Open the default managed workspace.
    ///
    /// The root is `HARNESS_JIT_DIR` when set, else
    /// `%TEMP%/harness-jit-workspace`.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when the directories cannot be created.
    pub fn open_default() -> Result<Self, JitError> {
        let root = std::env::var_os("HARNESS_JIT_DIR").map_or_else(
            || std::env::temp_dir().join("harness-jit-workspace"),
            PathBuf::from,
        );
        Self::open(&root)
    }

    /// The workspace root directory.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// The shared cargo target directory.
    #[must_use]
    pub fn target_dir(&self) -> PathBuf {
        self.root.join("target")
    }

    /// The directory holding one authored crate.
    #[must_use]
    pub fn crate_dir(&self, digest: &str) -> PathBuf {
        self.root.join("crates").join(digest)
    }

    /// Read the workspace manifest, ignoring unreadable content.
    #[must_use]
    pub fn read_manifest(&self) -> BTreeMap<String, WorkspaceManifest> {
        let Ok(text) = std::fs::read_to_string(self.root.join(MANIFEST_FILE)) else {
            return BTreeMap::new();
        };
        let Ok(parsed) = serde_json::from_str::<Json>(&text) else {
            return BTreeMap::new();
        };
        let Some(entries) = parsed.get("entries").and_then(Json::as_object) else {
            return BTreeMap::new();
        };
        entries
            .iter()
            .filter_map(|(digest, value)| {
                Some((
                    digest.clone(),
                    WorkspaceManifest {
                        kind: value.get("kind")?.as_str()?.to_string(),
                        crate_name: value.get("crate_name")?.as_str()?.to_string(),
                        artifact_relpath: value.get("artifact_relpath")?.as_str()?.to_string(),
                        toolchain_digest: value.get("toolchain_digest")?.as_str()?.to_string(),
                        sdk_version: value.get("sdk_version")?.as_str()?.to_string(),
                        built_at_unix: value.get("built_at_unix")?.as_u64()?,
                    },
                ))
            })
            .collect()
    }

    /// Replace the workspace manifest atomically.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when the replacement write fails.
    pub fn write_manifest(
        &self,
        entries: &BTreeMap<String, WorkspaceManifest>,
    ) -> Result<(), JitError> {
        let document = serde_json::json!({
            "entries": entries
                .iter()
                .map(|(digest, entry)| {
                    let value = serde_json::json!({
                        "kind": entry.kind,
                        "crate_name": entry.crate_name,
                        "artifact_relpath": entry.artifact_relpath,
                        "toolchain_digest": entry.toolchain_digest,
                        "sdk_version": entry.sdk_version,
                        "built_at_unix": entry.built_at_unix,
                    });
                    (digest.clone(), value)
                })
                .collect::<serde_json::Map<String, Json>>(),
        });
        let text = serde_json::to_string_pretty(&document)
            .map_err(|error| JitError::Io(format!("serializing the manifest: {error}")))?;
        let staging = self.root.join(format!("{MANIFEST_FILE}.tmp"));
        std::fs::write(&staging, text.as_bytes())
            .map_err(|error| JitError::Io(format!("staging the manifest: {error}")))?;
        std::fs::rename(&staging, self.root.join(MANIFEST_FILE))
            .map_err(|error| JitError::Io(format!("replacing the manifest: {error}")))?;
        Ok(())
    }
}
