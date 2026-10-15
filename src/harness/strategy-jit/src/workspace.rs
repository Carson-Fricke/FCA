//! The managed cargo workspace that holds authored crates and their builds.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::Value as Json;

use crate::error::JitError;

/// The toolchain pin copied into every managed workspace.
const TOOLCHAIN_FILE: &str = include_str!("../../rust-toolchain.toml");

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
    /// The SDK sources and local compiler contract used for the build.
    pub compiler_fingerprint: String,
    /// The SDK crate version compiled into the artifact.
    pub sdk_version: String,
    /// The SDK ABI version compiled into the artifact.
    pub sdk_abi_version: u32,
    /// The build completion time as seconds since the Unix epoch.
    pub built_at_unix: u64,
}

/// A managed cargo workspace with isolated build generations and locked manifests.
pub struct ManagedWorkspace {
    root: PathBuf,
}

#[cfg(windows)]
fn compiler_compatible_path(path: PathBuf) -> PathBuf {
    use std::ffi::OsString;
    use std::path::{Component, Prefix};

    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return path;
    };
    // MSVC linker/object paths require conventional DOS or UNC prefixes.
    let mut normalized = match prefix.kind() {
        Prefix::VerbatimDisk(drive) => PathBuf::from(format!("{}:\\", char::from(drive))),
        Prefix::VerbatimUNC(server, share) => {
            let mut root = OsString::from("\\\\");
            root.push(server);
            root.push("\\");
            root.push(share);
            root.push("\\");
            PathBuf::from(root)
        }
        _ => return path,
    };
    for component in components {
        if !matches!(component, Component::RootDir) {
            normalized.push(component.as_os_str());
        }
    }
    normalized
}

impl ManagedWorkspace {
    /// Open (or create) the managed workspace at `root`.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when the directories cannot be created or the
    /// toolchain pin cannot be written.
    pub fn open(root: &Path) -> Result<Self, JitError> {
        let root = std::path::absolute(root)?;
        std::fs::create_dir_all(&root)
            .map_err(|error| JitError::Io(format!("creating {}: {error}", root.display())))?;
        let root = root.canonicalize()?;
        #[cfg(windows)]
        let root = compiler_compatible_path(root);
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
        let workspace = Self { root };
        let _lock = workspace.lock("setup")?;
        let pin = workspace.root.join("rust-toolchain.toml");
        if std::fs::read_to_string(&pin).ok().as_deref() != Some(TOOLCHAIN_FILE) {
            std::fs::write(pin, TOOLCHAIN_FILE)
                .map_err(|error| JitError::Io(format!("writing the toolchain pin: {error}")))?;
        }
        Ok(workspace)
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

    /// The directory holding one authored crate generation.
    #[must_use]
    pub fn crate_dir(&self, digest: &str) -> PathBuf {
        self.root.join("crates").join(digest)
    }

    /// Locks are kernel-owned and released on close or process exit. Never unlink them.
    pub(crate) fn lock(&self, name: &str) -> Result<std::fs::File, JitError> {
        let directory = self.root.join("locks");
        std::fs::create_dir_all(&directory)?;
        let file = std::fs::File::options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(directory.join(format!("{name}.lock")))?;
        file.lock()?;
        Ok(file)
    }

    pub(crate) fn create_build_directory(&self) -> Result<String, JitError> {
        static SEQUENCE: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        loop {
            let nonce = format!(
                "{}:{}:{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0, |time| time.as_nanos()),
                SEQUENCE.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            );
            let name = backtest::sha256::digest_hex(nonce.as_bytes())[..16].to_string();
            match std::fs::create_dir(self.target_dir().join(&name)) {
                Ok(()) => return Ok(name),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error.into()),
            }
        }
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
                        compiler_fingerprint: value
                            .get("compiler_fingerprint")
                            .and_then(Json::as_str)
                            .unwrap_or_default()
                            .to_string(),
                        sdk_version: value.get("sdk_version")?.as_str()?.to_string(),
                        sdk_abi_version: u32::try_from(value.get("sdk_abi_version")?.as_u64()?)
                            .ok()?,
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
        let _lock = self.lock("manifest")?;
        self.write_manifest_locked(entries)
    }

    /// Merge one entry under the manifest lock so independent builds retain each other.
    pub fn update_manifest(
        &self,
        digest: String,
        entry: WorkspaceManifest,
    ) -> Result<(), JitError> {
        let _lock = self.lock("manifest")?;
        let mut entries = self.read_manifest();
        entries.insert(digest, entry);
        self.write_manifest_locked(&entries)
    }

    fn write_manifest_locked(
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
                        "compiler_fingerprint": entry.compiler_fingerprint,
                        "sdk_version": entry.sdk_version,
                        "sdk_abi_version": entry.sdk_abi_version,
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

#[cfg(test)]
#[path = "../tests/unit/workspace.rs"]
mod tests;
