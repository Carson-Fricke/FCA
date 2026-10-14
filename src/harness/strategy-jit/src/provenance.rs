//! Host, dependency, toolchain, and Cargo configuration identities.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Mutex, OnceLock};

use backtest::sha256::digest_hex;

use crate::error::JitError;

pub(super) fn compiler_fingerprint(
    workspace: &Path,
    sdk: &Path,
    toolchain: &str,
) -> Result<String, JitError> {
    let sdk = std::path::absolute(sdk)?;
    let mut inputs = vec![
        b"harness-jit-compiler-contract-v2".to_vec(),
        toolchain.as_bytes().to_vec(),
        env!("HARNESS_JIT_HOST_IDENTITY").as_bytes().to_vec(),
        env!("HARNESS_JIT_HOST_TARGET").as_bytes().to_vec(),
    ];
    let num = sdk.join("../num");
    let contracts = sdk.join("../contracts");
    let lake_contracts = sdk.join("../../lake/contracts");
    for directory in [
        sdk.as_path(),
        contracts.as_path(),
        num.as_path(),
        lake_contracts.as_path(),
    ] {
        fingerprint_file(&mut inputs, &directory.join("Cargo.toml"), false)?;
        fingerprint_file(&mut inputs, &directory.join("build.rs"), true)?;
        fingerprint_sources(&mut inputs, &directory.join("src"))?;
    }
    let workspace = std::path::absolute(workspace)?;
    fingerprint_configuration(&mut inputs, &workspace)?;
    let mut environment: Vec<_> = std::env::vars_os()
        .filter(|(name, _)| {
            let name = name.to_string_lossy();
            name.starts_with("CARGO_")
                || name.starts_with("RUST")
                || matches!(
                    name.as_ref(),
                    "PATH" | "CC" | "CXX" | "AR" | "LD" | "SDKROOT"
                )
        })
        .collect();
    environment.sort();
    for (name, value) in environment {
        inputs.push(name.as_encoded_bytes().to_vec());
        inputs.push(value.as_encoded_bytes().to_vec());
    }
    let mut tools = vec![std::ffi::OsString::from("cargo")];
    if let Some(compiler) =
        std::env::var_os("RUSTC").or_else(|| std::env::var_os("CARGO_BUILD_RUSTC"))
    {
        tools.push(compiler);
    }
    for tool in tools {
        fingerprint_executable(&mut inputs, &tool)?;
        inputs.push(compiler_probe(
            &tool,
            &["--version", "--verbose"],
            &workspace,
        )?);
    }
    for tool in ["rustup", "rustc"] {
        fingerprint_executable(&mut inputs, tool.as_ref())?;
    }
    for tool in ["rustc", "cargo"] {
        let output = compiler_probe("rustup".as_ref(), &["which", tool], &workspace)?;
        let path = std::str::from_utf8(&output)
            .map_err(|error| JitError::Io(error.to_string()))?
            .trim();
        fingerprint_executable(&mut inputs, Path::new(path).as_os_str())?;
    }
    let bytes = serde_json::to_vec(&inputs)
        .map_err(|error| JitError::Io(format!("encoding compiler fingerprint: {error}")))?;
    Ok(digest_hex(&bytes))
}

fn fingerprint_configuration(inputs: &mut Vec<Vec<u8>>, workspace: &Path) -> Result<(), JitError> {
    for directory in workspace.ancestors() {
        for name in [
            ".cargo/config",
            ".cargo/config.toml",
            "rust-toolchain",
            "rust-toolchain.toml",
        ] {
            fingerprint_file(inputs, &directory.join(name), true)?;
        }
    }
    let cargo_home = std::env::var_os("CARGO_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
                .map(|home| PathBuf::from(home).join(".cargo"))
        })
        .ok_or_else(|| JitError::Io("cannot resolve Cargo home".to_string()))?;
    for name in ["config", "config.toml"] {
        fingerprint_file(inputs, &cargo_home.join(name), true)?;
    }
    Ok(())
}

/// Cache successful probes while freshly checking settings and executable metadata.
pub(crate) fn compiler_probe(
    tool: &std::ffi::OsStr,
    args: &[&str],
    workspace: &Path,
) -> Result<Vec<u8>, JitError> {
    let rustup_home = std::env::var_os("RUSTUP_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
                .map(|home| PathBuf::from(home).join(".rustup"))
        })
        .ok_or_else(|| JitError::Io("cannot resolve Rustup home".to_string()))?;
    compiler_probe_with_settings(tool, args, workspace, &rustup_home.join("settings.toml"))
}

fn compiler_probe_with_settings(
    tool: &std::ffi::OsStr,
    args: &[&str],
    workspace: &Path,
    settings: &Path,
) -> Result<Vec<u8>, JitError> {
    type ProbeCache = VecDeque<(String, Vec<u8>)>;
    static CACHE: OnceLock<Mutex<ProbeCache>> = OnceLock::new();
    let mut inputs = vec![
        tool.as_encoded_bytes().to_vec(),
        args.join("\0").into_bytes(),
    ];
    fingerprint_executable(&mut inputs, tool)?;
    fingerprint_executable(&mut inputs, "rustup".as_ref())?;
    fingerprint_file(&mut inputs, &std::path::absolute(settings)?, true)?;
    let workspace = std::path::absolute(workspace)?;
    inputs.push(workspace.as_os_str().as_encoded_bytes().to_vec());
    fingerprint_configuration(&mut inputs, &workspace)?;
    let mut environment: Vec<_> = std::env::vars_os().collect();
    environment.sort();
    for (name, value) in environment {
        inputs.push(name.as_encoded_bytes().to_vec());
        inputs.push(value.as_encoded_bytes().to_vec());
    }
    if args.first() != Some(&"which") {
        for name in ["rustc", "cargo"] {
            let path = compiler_probe_with_settings(
                "rustup".as_ref(),
                &["which", name],
                &workspace,
                settings,
            )?;
            let path =
                std::str::from_utf8(&path).map_err(|error| JitError::Io(error.to_string()))?;
            fingerprint_executable(&mut inputs, Path::new(path.trim()).as_os_str())?;
        }
    }
    let key =
        digest_hex(&serde_json::to_vec(&inputs).map_err(|error| JitError::Io(error.to_string()))?);
    let mut cache = CACHE
        .get_or_init(|| Mutex::new(VecDeque::new()))
        .lock()
        .map_err(|_| JitError::Io("compiler probe cache poisoned".into()))?;
    if let Some((_, output)) = cache.iter().find(|(stored, _)| stored == &key) {
        if args.first() == Some(&"which") {
            let path =
                std::str::from_utf8(output).map_err(|error| JitError::Io(error.to_string()))?;
            fingerprint_executable(&mut Vec::new(), Path::new(path.trim()).as_os_str())?;
        }
        return Ok(output.clone());
    }
    #[cfg(test)]
    tests::record_probe();
    let output = Command::new(tool)
        .args(args)
        .current_dir(&workspace)
        .output()?;
    if !output.status.success() || output.stdout.is_empty() {
        return Err(JitError::Io(format!(
            "probing {tool:?}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    if cache.len() >= 32 {
        cache.pop_front();
    }
    cache.push_back((key, output.stdout.clone()));
    Ok(output.stdout)
}

fn fingerprint_executable(
    inputs: &mut Vec<Vec<u8>>,
    program: &std::ffi::OsStr,
) -> Result<(), JitError> {
    let program = Path::new(program);
    let path = if program.is_absolute() || program.components().count() > 1 {
        program.canonicalize()?
    } else {
        let search = std::env::var_os("PATH")
            .ok_or_else(|| JitError::Io("cannot resolve compiler PATH".to_string()))?;
        std::env::split_paths(&search)
            .map(|directory| {
                let mut path = directory.join(program);
                if cfg!(windows) && path.extension().is_none() {
                    path.set_extension("exe");
                }
                path
            })
            .find(|path| path.is_file())
            .ok_or_else(|| JitError::Io(format!("cannot resolve {}", program.display())))?
            .canonicalize()?
    };
    let metadata = path.metadata()?;
    inputs.push(path.as_os_str().as_encoded_bytes().to_vec());
    inputs.push(metadata.len().to_le_bytes().to_vec());
    inputs.push(
        metadata
            .modified()?
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|error| JitError::Io(error.to_string()))?
            .as_nanos()
            .to_le_bytes()
            .to_vec(),
    );
    Ok(())
}

fn fingerprint_file(
    inputs: &mut Vec<Vec<u8>>,
    path: &Path,
    optional: bool,
) -> Result<(), JitError> {
    inputs.push(path.as_os_str().as_encoded_bytes().to_vec());
    match std::fs::read(path) {
        Ok(bytes) => {
            inputs.push(b"present".to_vec());
            inputs.push(bytes);
            Ok(())
        }
        Err(error) if optional && error.kind() == std::io::ErrorKind::NotFound => {
            inputs.push(b"absent".to_vec());
            Ok(())
        }
        Err(error) => Err(JitError::Io(format!(
            "fingerprinting {}: {error}",
            path.display()
        ))),
    }
}

fn fingerprint_sources(inputs: &mut Vec<Vec<u8>>, directory: &Path) -> Result<(), JitError> {
    let mut entries = std::fs::read_dir(directory)?.collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(|entry| entry.file_name());
    if entries.is_empty() {
        return Err(JitError::Io("empty SDK source directory".to_string()));
    }
    for entry in entries {
        let kind = entry.file_type()?;
        if kind.is_dir() {
            fingerprint_sources(inputs, &entry.path())?;
        } else if kind.is_file() {
            fingerprint_file(inputs, &entry.path(), false)?;
        } else {
            return Err(JitError::Io("unsupported SDK source file type".to_string()));
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "../tests/unit/provenance.rs"]
mod tests;
