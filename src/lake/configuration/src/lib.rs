//! Strict YAML-subset loading, environment overrides, and validated lake settings.

pub(crate) mod env;
pub mod error;
pub mod settings;
pub mod universes;
pub mod yaml;

use std::path::{Path, PathBuf};

pub use error::ConfigError;
pub use settings::{
    ApiSettings, AuthSettings, BackfillSettings, ColdSettings, IngestSettings, LakeSettings,
    LifecycleSettings, OperationsSettings, ParquetSettings, ProviderSettings, QueueSettings,
    RequestSettings, RetrySettings, SasSettings, SqlSettings, StorageSettings,
};
pub use universes::{
    CryptoRollout, CryptoUniverse, EquityHistory, EquityUniverse, HistoryPolicy, OptionSelection,
    OptionsExpansion, OptionsSoak, OptionsUniverse, StrikeWindow, UniverseSettings,
};
pub use yaml::{load_yaml_mapping, parse_yaml};

/// Resolve the market-lake config directory without machine-specific paths.
///
/// Mirrors `lake.configuration.loader.market_lake_config_dir`: an explicit root
/// may point directly at `config/market-lake`, otherwise the repository root is
/// used with `config/market-lake` appended, and `LAKE_CONFIG_ROOT` overrides
/// the fallback repository root.
pub fn market_lake_config_dir(
    root: Option<&Path>,
    environ: &std::collections::BTreeMap<String, String>,
) -> PathBuf {
    let root_path = match root {
        Some(path) => path.to_path_buf(),
        None => match env::lookup(environ, "LAKE_CONFIG_ROOT").filter(|value| !value.is_empty()) {
            Some(configured) => PathBuf::from(configured),
            None => repository_root(),
        },
    };
    if root_path
        .file_name()
        .is_some_and(|name| name == "market-lake")
        && root_path
            .parent()
            .and_then(Path::file_name)
            .is_some_and(|name| name == "config")
    {
        return root_path;
    }
    root_path.join("config").join("market-lake")
}

/// The repository root as resolved at compile time from the crate location.
fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("crate manifest lives under <root>/src/lake/configuration")
        .to_path_buf()
}

#[cfg(test)]
#[path = "../tests/unit/lib.rs"]
mod tests;
