//! Toolchain resolution for managed builds.

use std::path::Path;

use backtest::sha256::digest_hex;

use crate::error::JitError;

/// The resolved toolchain driving managed builds.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Toolchain {
    /// The compiler release line, for example `1.90.0`.
    pub release: String,
    /// The compiler commit hash.
    pub commit_hash: String,
    /// The compiler host triple.
    pub host: String,
    /// The SHA-256 digest over the full `rustc --version --verbose` output.
    pub digest: String,
}

/// Resolve the toolchain a managed workspace builds with.
///
/// Runs `rustc --version --verbose` with the workspace as the working
/// directory so `rustup` resolves the workspace's pinned `rust-toolchain.toml`.
///
/// # Errors
///
/// Returns [`JitError`] when `rustc` cannot be executed or reports nothing.
pub fn resolve_toolchain(cwd: &Path) -> Result<Toolchain, JitError> {
    let output =
        crate::provenance::compiler_probe("rustc".as_ref(), &["--version", "--verbose"], cwd)?;
    let text = String::from_utf8_lossy(&output).into_owned();
    let mut release = String::new();
    let mut commit_hash = String::new();
    let mut host = String::new();
    for line in text.lines() {
        if let Some((key, value)) = line.split_once(": ") {
            match key {
                "release" => release = value.trim().to_string(),
                "commit-hash" => commit_hash = value.trim().to_string(),
                "host" => host = value.trim().to_string(),
                _ => {}
            }
        }
    }
    if release.is_empty() || host.is_empty() {
        return Err(JitError::Io(format!(
            "rustc --version reported an unusable header: {}",
            text.trim()
        )));
    }
    Ok(Toolchain {
        digest: digest_hex(text.as_bytes()),
        release,
        commit_hash,
        host,
    })
}
