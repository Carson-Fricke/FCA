use crate::{CompiledArtifact, ManagedWorkspace};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub const SOURCE: &str =
    "#[no_mangle] pub extern \"C\" fn contract_value() -> u32 { harness_strategy_sdk::value() }";

pub struct Fixture {
    pub root: PathBuf,
    pub sdk: PathBuf,
    pub workspace: ManagedWorkspace,
}

impl Fixture {
    pub fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "harness-jit-contract-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let sdk = root.join("harness/strategy-sdk");
        for (name, package, source) in [
            (
                "harness/strategy-sdk",
                "harness-strategy-sdk",
                "pub fn value() -> u32 { num::VALUE + 1 }",
            ),
            ("harness/num", "num", "pub const VALUE: u32 = 1;"),
            (
                "harness/contracts",
                "harness-contracts",
                "pub const VALUE: u32 = lake_contracts::VALUE;",
            ),
            (
                "lake/contracts",
                "lake-contracts",
                "pub const VALUE: u32 = 1;",
            ),
        ] {
            let directory = root.join(name);
            std::fs::create_dir_all(directory.join("src")).unwrap();
            let dependencies = if package == "harness-strategy-sdk" {
                "[dependencies]\nnum = { path = \"../num\" }\nharness-contracts = { path = \"../contracts\" }\n"
            } else if package == "harness-contracts" {
                "[dependencies]\nlake-contracts = { path = \"../../lake/contracts\" }\n"
            } else {
                ""
            };
            std::fs::write(directory.join("Cargo.toml"), format!("[package]\nname = \"{package}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n{dependencies}")).unwrap();
            std::fs::write(directory.join("src/lib.rs"), source).unwrap();
        }
        let workspace = ManagedWorkspace::open(&root.join("managed")).unwrap();
        Self {
            root,
            sdk,
            workspace,
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

pub fn artifact_value(artifact: &CompiledArtifact) -> u32 {
    unsafe {
        let library = libloading::Library::new(&artifact.artifact_path).unwrap();
        let value = library
            .get::<unsafe extern "C" fn() -> u32>(b"contract_value")
            .unwrap();
        value()
    }
}

pub fn replace_preserving_mtime(path: &Path, source: &str) {
    let modified = path.metadata().unwrap().modified().unwrap();
    std::fs::write(path, source).unwrap();
    std::fs::File::options()
        .write(true)
        .open(path)
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(modified))
        .unwrap();
    assert_eq!(path.metadata().unwrap().modified().unwrap(), modified);
}
