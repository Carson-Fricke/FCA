//! Durable admission and isolated finite execution; market acquisition stays in
//! the hosted market-data capability and simulation stays in the native engine.
pub mod backend;
pub mod dispatch;
pub mod launch;
pub mod renewal;
pub mod store;
pub mod validation;
pub mod worker;
use backend::{error, Result};
use std::sync::Arc;
pub use store::ExecutionStore;

pub trait Wake: Send + Sync {
    fn wake(&self) -> std::result::Result<(), String>;
}

pub fn configured_store() -> Result<ExecutionStore> {
    let mode = std::env::var("HARNESS_MARKET_DATA_MODE").unwrap_or_else(|_| "local".into());
    let backend: Arc<dyn backend::Backend> = match mode.as_str() {
        "local" => Arc::new(backend::Local {
            root: crate::http::state::results_root_from_env(),
        }),
        "azure" => {
            let account = std::env::var("LAKE_STORAGE_ACCOUNT_NAME").map_err(error)?;
            let container = std::env::var("HARNESS_RESULTS_CONTAINER").map_err(error)?;
            let credential = Arc::new(
                azure_identity::DefaultAzureCredentialBuilder::new()
                    .build()
                    .map_err(error)?,
            );
            let account_url = format!("https://{account}.blob.core.windows.net");
            Arc::new(backend::Azure {
                container: lake_storage::azure::container_from_account_url(
                    &account_url,
                    &container,
                    credential.clone(),
                )
                .map_err(error)?,
                sas: Arc::new(
                    lake_storage::sas_issuer_from_account_url(
                        &account_url,
                        &container,
                        credential,
                        900,
                    )
                    .map_err(error)?,
                ),
            })
        }
        _ => return Err(error("HARNESS_MARKET_DATA_MODE must be local or azure")),
    };
    Ok(ExecutionStore::new(backend))
}

pub fn composed() -> Result<crate::http::state::AppConfig> {
    let mut config = crate::http::state::AppConfig::in_memory();
    let store = Arc::new(configured_store()?);
    let wake = Arc::new(dispatch::ConfiguredWake);
    config.job_store = store;
    config.launcher = Arc::new(dispatch::QueuedLauncher(wake.clone()));
    config.screener_launcher = Arc::new(dispatch::QueuedLauncher(wake.clone()));
    config.mcp_server = Some(
        crate::http::mcp::compose_queued_server(config.job_store.clone(), wake).map_err(error)?,
    );
    Ok(config)
}

/// Derived control work only. An API restart loses no accepted execution; Azure
/// also runs this reconciliation from a scheduled, independently hosted job.
pub fn start_control_reconciler() -> tokio::task::JoinHandle<()> {
    tokio::spawn(async {
        loop {
            let result = tokio::task::spawn_blocking(|| -> backend::Result<()> {
                let store = configured_store()?;
                store.recover_interrupted()?;
                if !store.queued()?.is_empty() {
                    let max = std::env::var("HARNESS_EXECUTION_MAX_WORKERS")
                        .ok()
                        .map(|s| s.parse::<usize>().map_err(backend::error))
                        .transpose()?
                        .unwrap_or(2);
                    if let Some(slot) = worker::Slot::acquire(&store, max)? {
                        drop(slot);
                        dispatch::ConfiguredWake.wake().map_err(backend::error)?;
                    }
                }
                Ok(())
            })
            .await;
            if let Err(error) = result.unwrap_or_else(|e| Err(backend::error(e))) {
                eprintln!("execution control reconciliation failed: {error}");
            }
            tokio::time::sleep(std::time::Duration::from_secs(10)).await;
        }
    })
}
