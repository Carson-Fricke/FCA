//! Embed the complete host implementation identity without runtime source access.

#[allow(dead_code)]
#[path = "../backtest/src/sha256.rs"]
mod sha256;

use std::path::Path;

fn hash_tree(root: &Path, path: &Path, hash: &mut sha256::Sha256) {
    println!("cargo:rerun-if-changed={}", path.display());
    if path.is_dir() {
        let mut entries = std::fs::read_dir(path)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect::<Vec<_>>();
        entries.sort();
        for entry in entries {
            hash_tree(root, &entry, hash);
        }
    } else {
        let name = path
            .strip_prefix(root)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        let bytes = std::fs::read(path).unwrap();
        hash.update(&(name.len() as u64).to_le_bytes());
        hash.update(name.as_bytes());
        hash.update(&(bytes.len() as u64).to_le_bytes());
        hash.update(&bytes);
    }
}

fn main() {
    let root = std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let mut hash = sha256::Sha256::new();
    for path in ["Cargo.toml", "build.rs", "src"] {
        hash_tree(&root, &root.join(path), &mut hash);
    }
    println!("cargo:rerun-if-changed=../backtest/src/sha256.rs");
    println!(
        "cargo:rustc-env=HARNESS_JIT_HOST_IDENTITY={}",
        hash.finalize_hex()
    );
    println!(
        "cargo:rustc-env=HARNESS_JIT_HOST_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
}
