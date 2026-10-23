//! Path construction for recorded run artifacts.

use std::path::Path;
use std::path::PathBuf;

use super::schemas::Artifact;

/// Return the artifact file path inside a run directory.
#[must_use]
pub fn artifact_path(directory: impl AsRef<Path>, artifact: Artifact) -> PathBuf {
    directory.as_ref().join(artifact.file_name())
}
