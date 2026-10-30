//! Owns marker-free local conditional objects, file streaming and atomic replacement.
//! Execution lifecycle and artifact manifests belong to harness-execution.
use harness_execution::jobs::StoreError;
use harness_execution::persistence::{error, Backend, Result};
use std::path::{Path, PathBuf};

/// Publish a synced temporary file under the caller's object lock. Creation
/// never clobbers; replacement keeps readers of the previous file usable.
fn publish(temporary: tempfile::NamedTempFile, target: &Path, replace: bool) -> Result<()> {
    let result = if replace {
        replace_existing(temporary, target)
    } else {
        temporary
            .persist_noclobber(target)
            .map(|_| ())
            .map_err(|failure| failure.error)
    };
    result.map_err(|failure| {
        error(format!(
            "failed to {} local object '{}': {failure}",
            if replace { "replace" } else { "create" },
            target.display()
        ))
    })
}

#[cfg(not(windows))]
fn replace_existing(temporary: tempfile::NamedTempFile, target: &Path) -> std::io::Result<()> {
    temporary
        .persist(target)
        .map(|_| ())
        .map_err(|failure| failure.error)
}

#[cfg(windows)]
fn replace_existing(temporary: tempfile::NamedTempFile, target: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::{
        ERROR_UNABLE_TO_MOVE_REPLACEMENT, ERROR_UNABLE_TO_MOVE_REPLACEMENT_2,
    };
    use windows_sys::Win32::Storage::FileSystem::ReplaceFileW;

    // Close our staging handle before replacement; TempPath still owns cleanup
    // on failure. Rename-based persistence fails with ordinary open readers on
    // Windows, even when they allow delete sharing.
    let mut temporary = temporary.into_temp_path();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    let source: Vec<u16> = temporary.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: both paths are owned NUL-terminated buffers valid through the call;
    // no backup or optional parameters are supplied.
    let replaced = unsafe {
        ReplaceFileW(
            target.as_ptr(),
            source.as_ptr(),
            std::ptr::null(),
            0,
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    if replaced == 0 {
        let failure = std::io::Error::last_os_error();
        // These documented partial failures can move/remove the old target.
        // Retain the staged bytes for reconciliation rather than delete what
        // may now be the only copy. Never retry an uncertain publication.
        if matches!(
            failure.raw_os_error().map(|code| code as u32),
            Some(ERROR_UNABLE_TO_MOVE_REPLACEMENT | ERROR_UNABLE_TO_MOVE_REPLACEMENT_2)
        ) {
            temporary.disable_cleanup(true);
            return Err(std::io::Error::new(
                failure.kind(),
                format!(
                    "{failure}; staged replacement retained at '{}'",
                    temporary.display()
                ),
            ));
        }
        Err(failure)
    } else {
        Ok(())
    }
}

pub struct Local {
    pub root: PathBuf,
}
impl Local {
    fn lock(&self, path: &str) -> Result<std::fs::File> {
        use sha2::{Digest, Sha256};
        let dir = self.root.join("locks");
        std::fs::create_dir_all(&dir).map_err(error)?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(dir.join(hex::encode(Sha256::digest(path.as_bytes()))))
            .map_err(error)?;
        lock.lock().map_err(error)?;
        Ok(lock)
    }
    fn write(&self, path: &str, bytes: &[u8], replace: bool) -> Result<()> {
        use std::io::Write;
        let target = self.root.join(path);
        let parent = target.parent().unwrap();
        std::fs::create_dir_all(parent).map_err(error)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(error)?;
        temporary.write_all(bytes).map_err(error)?;
        temporary.as_file().sync_all().map_err(error)?;
        publish(temporary, &target, replace)
    }
}
impl Backend for Local {
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()> {
        let _guard = self.lock(path)?;
        let target = self.root.join(path);
        if target.exists() {
            return if target.is_file() {
                Ok(())
            } else {
                Err(error("artifact object is not a file"))
            };
        }
        let parent = target.parent().unwrap();
        std::fs::create_dir_all(parent).map_err(error)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(error)?;
        std::io::copy(
            &mut std::fs::File::open(source).map_err(error)?,
            temporary.as_file_mut(),
        )
        .map_err(error)?;
        temporary.as_file().sync_all().map_err(error)?;
        publish(temporary, &target, false)
    }

    fn open(&self, path: &str) -> Result<Box<dyn std::io::Read + Send>> {
        Ok(Box::new(
            std::fs::File::open(self.root.join(path)).map_err(error)?,
        ))
    }
    fn read(&self, path: &str) -> Result<Option<Vec<u8>>> {
        match std::fs::read(self.root.join(path)) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(error(e)),
        }
    }
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool> {
        let _guard = self.lock(path)?;
        if self.read(path)?.is_some() {
            return Ok(false);
        }
        self.write(path, bytes, false)?;
        Ok(true)
    }
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()> {
        let _guard = self.lock(path)?;
        let bytes = self
            .read(path)?
            .ok_or_else(|| StoreError::JobNotFound(path.into()))?;
        self.write(path, &change(bytes)?, true)
    }
    fn list(&self, prefix: &str) -> Result<Vec<String>> {
        fn walk(
            root: &std::path::Path,
            dir: &std::path::Path,
            out: &mut Vec<String>,
        ) -> Result<()> {
            if !dir.exists() {
                return Ok(());
            }
            for entry in std::fs::read_dir(dir).map_err(error)? {
                let path = entry.map_err(error)?.path();
                if path.is_dir() {
                    walk(root, &path, out)?;
                } else {
                    out.push(
                        path.strip_prefix(root)
                            .map_err(error)?
                            .to_string_lossy()
                            .replace('\\', "/"),
                    );
                }
            }
            Ok(())
        }
        let mut out = Vec::new();
        walk(
            &self.root,
            &self.root.join(prefix.trim_end_matches('/')),
            &mut out,
        )?;
        out.sort();
        Ok(out)
    }
    fn delete(&self, path: &str) -> Result<()> {
        let _guard = self.lock(path)?;
        match std::fs::remove_file(self.root.join(path)) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(error(e)),
        }
    }
}
