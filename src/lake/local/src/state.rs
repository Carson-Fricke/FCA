//! Owned development filesystem layout and explicit marker-protected cleanup.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::{Component, Path, PathBuf};

/// The ownership marker file name inside the local data root.
pub const MARKER_NAME: &str = ".market-lake-local";

/// The exact marker content an initialized local root must carry.
pub const MARKER_CONTENT: &str = "market-lake-local-v1\n";

const DATA_DIR_ENV: &str = "LAKE_LOCAL_DATA_DIR";

/// Local state is unsafe, unavailable, or malformed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LocalStateError {
    /// Human-readable failure detail.
    pub message: String,
}

impl LocalStateError {
    /// Build an error with the supplied detail.
    pub(crate) fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for LocalStateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for LocalStateError {}

/// Repository-ignored directories owned by one local market-lake instance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LocalLayout {
    root: PathBuf,
}

impl LocalLayout {
    /// Build a layout rooted at `root`, absolutized and lexically normalized.
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: resolve(root.into()),
        }
    }

    /// Resolve an explicit path, environment override, or repository default.
    ///
    /// The explicit `data_dir` wins over `LAKE_LOCAL_DATA_DIR`; an empty
    /// configured value is rejected; with neither the repository root's
    /// `.local/market-lake` directory is used, exactly as Python does.
    pub fn from_environment(
        data_dir: Option<&Path>,
        environ: &BTreeMap<String, String>,
    ) -> Result<Self, LocalStateError> {
        let configured = match data_dir {
            Some(path) => Some(path.to_path_buf()),
            None => lookup(environ, DATA_DIR_ENV).map(PathBuf::from),
        };
        if let Some(path) = &configured {
            if path.as_os_str().to_string_lossy().trim().is_empty() {
                return Err(LocalStateError::new(
                    "LAKE_LOCAL_DATA_DIR must not be empty",
                ));
            }
        }
        let root =
            configured.unwrap_or_else(|| repository_root().join(".local").join("market-lake"));
        Ok(Self {
            root: resolve(root),
        })
    }

    /// The resolved local data root.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// The Parquet object root below the data root.
    pub fn objects(&self) -> PathBuf {
        self.root.join("objects")
    }

    /// The local state directory below the data root.
    pub fn state_dir(&self) -> PathBuf {
        self.root.join("state")
    }

    /// The ownership marker path.
    pub fn marker_path(&self) -> PathBuf {
        self.root.join(MARKER_NAME)
    }

    /// Whether the root is an initialized, marker-valid local tree.
    pub fn is_initialized(&self) -> bool {
        self.root.is_dir() && marker_matches(&self.marker_path())
    }

    /// Create or validate a directory dedicated to local market-lake state.
    pub fn initialize(&self) -> Result<(), LocalStateError> {
        let root = &self.root;
        if root.exists() && !root.is_dir() {
            return Err(LocalStateError::new(format!(
                "local data path is not a directory: {}",
                root.display()
            )));
        }
        let marker = self.marker_path();
        if root.is_dir() && !marker.exists() {
            let empty = match fs::read_dir(root) {
                Ok(mut entries) => entries.next().is_none(),
                Err(error) => {
                    return Err(LocalStateError::new(format!(
                        "cannot inspect local data directory: {} ({error})",
                        root.display()
                    )));
                }
            };
            if !empty {
                return Err(LocalStateError::new(format!(
                    "refusing to use a non-empty directory not created by local mode: {}",
                    root.display()
                )));
            }
        }
        fs::create_dir_all(root).map_err(|error| {
            LocalStateError::new(format!(
                "cannot create local data directory: {} ({error})",
                root.display()
            ))
        })?;
        if marker.exists() {
            if !marker.is_file() || !marker_matches(&marker) {
                return Err(LocalStateError::new(format!(
                    "invalid local data marker: {}",
                    marker.display()
                )));
            }
        } else {
            fs::write(&marker, MARKER_CONTENT).map_err(|error| {
                LocalStateError::new(format!(
                    "cannot write the local data marker: {} ({error})",
                    marker.display()
                ))
            })?;
        }
        for directory in [self.objects(), self.state_dir()] {
            fs::create_dir_all(&directory).map_err(|error| {
                LocalStateError::new(format!(
                    "cannot create local data directory: {} ({error})",
                    directory.display()
                ))
            })?;
        }
        Ok(())
    }

    /// Remove a previously initialized local tree and nothing else.
    ///
    /// An absent root is a no-op; a root without the valid ownership marker is
    /// never touched.
    pub fn reset(&self) -> Result<(), LocalStateError> {
        let root = &self.root;
        if !root.exists() {
            return Ok(());
        }
        let marker = self.marker_path();
        if !root.is_dir() || !marker.is_file() {
            return Err(LocalStateError::new(format!(
                "refusing to reset a directory without the local-mode marker: {}",
                root.display()
            )));
        }
        if !marker_matches(&marker) {
            return Err(LocalStateError::new(format!(
                "invalid local data marker: {}",
                marker.display()
            )));
        }
        fs::remove_dir_all(root).map_err(|error| {
            LocalStateError::new(format!(
                "cannot remove local data directory: {} ({error})",
                root.display()
            ))
        })
    }
}

/// Case-insensitive environment lookup mirroring the configuration loader.
pub(crate) fn lookup(environ: &BTreeMap<String, String>, key: &str) -> Option<String> {
    if let Some(value) = environ.get(key) {
        return Some(value.clone());
    }
    let wanted = key.to_uppercase();
    environ
        .iter()
        .find(|(candidate, _)| candidate.to_uppercase() == wanted)
        .map(|(_, value)| value.clone())
}

/// Whether the marker file exists with exactly the pinned content.
fn marker_matches(marker: &Path) -> bool {
    marker.is_file()
        && fs::read(marker)
            .map(|bytes| bytes == MARKER_CONTENT.as_bytes())
            .unwrap_or(false)
}

/// The repository root as resolved at compile time from the crate location.
fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("crate manifest lives under <root>/src/lake/local")
        .to_path_buf()
}

/// Absolutize a path against the working directory and remove `.`/`..` parts.
fn resolve(path: PathBuf) -> PathBuf {
    let absolute = if path.is_absolute() {
        path
    } else {
        match std::env::current_dir() {
            Ok(working) => working.join(path),
            Err(_) => path,
        }
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
        }
    }
    normalized
}
