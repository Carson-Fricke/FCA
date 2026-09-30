//! Atomic local filesystem object store.
//!
//! Ports `lake.storage.parquet.LocalFilesystemStore`: objects live below a
//! resolved root, every path is validated as a relative POSIX path that stays
//! under that root, and replacement writes a temporary file in the target
//! directory, fsyncs it, and renames it into place. The rename is retried on
//! Windows sharing violations because an open handle denies the replace.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use lake_contracts::DataError;

use crate::ports::{BlobStore, ReadableBlobStore};

const PATH_ERROR: &str = "object path must be a non-empty relative POSIX path";
const RENAME_ATTEMPTS: u64 = 6;
const RENAME_BACKOFF_MILLIS: u64 = 50;

static TEMPORARY_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Store objects below a local root using atomic filesystem replacement.
#[derive(Debug)]
pub struct LocalFilesystemStore {
    root: PathBuf,
}

impl LocalFilesystemStore {
    /// Create the root directory when needed and resolve it.
    pub fn new(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        let _ = fs::create_dir_all(&root);
        let root = fs::canonicalize(&root).unwrap_or(root);
        Self { root }
    }

    /// The resolved store root.
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn target(&self, relative_path: &str) -> Result<PathBuf, DataError> {
        let parts = validate_relative_path(relative_path)?;
        let mut target = self.root.clone();
        for part in parts {
            target.push(part);
        }
        if !target.starts_with(&self.root) {
            return Err(DataError::value("object path escapes the store root"));
        }
        Ok(target)
    }

    fn prefix_directory(&self, prefix: &str) -> Result<PathBuf, DataError> {
        if prefix.is_empty() {
            return Ok(self.root.clone());
        }
        self.target(prefix)
    }

    fn collect_files(directory: &Path, files: &mut Vec<PathBuf>) -> Result<(), DataError> {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let file_type = entry.file_type()?;
            if file_type.is_dir() {
                Self::collect_files(&entry.path(), files)?;
            } else if file_type.is_file() {
                files.push(entry.path());
            }
        }
        Ok(())
    }

    fn relative_posix(root: &Path, path: &Path) -> Result<String, DataError> {
        let relative = path
            .strip_prefix(root)
            .map_err(|_| DataError::value("object path escapes the store root"))?;
        let parts: Vec<String> = relative
            .components()
            .map(|component| component.as_os_str().to_string_lossy().into_owned())
            .collect();
        Ok(parts.join("/"))
    }

    fn objects_below(&self, prefix: &str) -> Result<Vec<PathBuf>, DataError> {
        let start = self.prefix_directory(prefix)?;
        let mut files = Vec::new();
        if start.is_file() {
            files.push(start);
        } else if start.is_dir() {
            Self::collect_files(&start, &mut files)?;
        }
        files.sort();
        Ok(files)
    }
}

impl BlobStore for LocalFilesystemStore {
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
        let target = self.target(relative_path)?;
        let parent = target
            .parent()
            .ok_or_else(|| DataError::value(PATH_ERROR))?
            .to_path_buf();
        fs::create_dir_all(&parent)?;
        let name = target
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        let temporary = parent.join(format!(".{name}.{}.tmp", random_suffix()));
        let outcome = write_and_replace(&temporary, &target, payload);
        if outcome.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        outcome.map_err(DataError::io)
    }
}

impl ReadableBlobStore for LocalFilesystemStore {
    fn read(&self, relative_path: &str) -> Result<Vec<u8>, DataError> {
        Ok(fs::read(self.target(relative_path)?)?)
    }

    fn list_prefix(&self, prefix: &str) -> Result<Vec<String>, DataError> {
        let mut objects = Vec::new();
        for path in self.objects_below(prefix)? {
            objects.push(Self::relative_posix(&self.root, &path)?);
        }
        Ok(objects)
    }

    fn delete_prefix(&self, prefix: &str) -> Result<u64, DataError> {
        let start = self.prefix_directory(prefix)?;
        let files = self.objects_below(prefix)?;
        let mut removed = 0u64;
        for path in &files {
            fs::remove_file(path)?;
            removed += 1;
        }
        if start.is_dir() {
            let _ = fs::remove_dir_all(&start);
        }
        Ok(removed)
    }
}

fn validate_relative_path(relative_path: &str) -> Result<Vec<&str>, DataError> {
    if relative_path.is_empty() || relative_path.contains('\\') || relative_path.starts_with('/') {
        return Err(DataError::value(PATH_ERROR));
    }
    let parts: Vec<&str> = relative_path.split('/').collect();
    if parts
        .iter()
        .any(|part| part.is_empty() || *part == "." || *part == "..")
    {
        return Err(DataError::value(PATH_ERROR));
    }
    Ok(parts)
}

fn write_and_replace(temporary: &Path, target: &Path, payload: &[u8]) -> std::io::Result<()> {
    {
        let mut file = fs::File::create(temporary)?;
        file.write_all(payload)?;
        file.flush()?;
        file.sync_all()?;
    }
    rename_with_retries(temporary, target)
}

fn rename_with_retries(temporary: &Path, target: &Path) -> std::io::Result<()> {
    for attempt in 0..RENAME_ATTEMPTS {
        match fs::rename(temporary, target) {
            Ok(()) => return Ok(()),
            Err(error) if attempt + 1 < RENAME_ATTEMPTS && is_sharing_violation(&error) => {
                std::thread::sleep(Duration::from_millis(RENAME_BACKOFF_MILLIS * (attempt + 1)));
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!("the retry loop returns on the final attempt")
}

fn is_sharing_violation(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::PermissionDenied | std::io::ErrorKind::Other
    ) && matches!(error.raw_os_error(), Some(32) | Some(5))
}

fn random_suffix() -> String {
    let counter = TEMPORARY_COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0);
    format!("{nanos:032x}{counter:08x}")
}
