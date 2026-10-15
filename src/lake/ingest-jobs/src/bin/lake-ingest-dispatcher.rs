//! Finite coordination process: reconcile durable work without acquiring provider data.
use lake_ingest_jobs::{AciConfig, AciRunner, IngestionJobs, JobStore};
use std::{collections::BTreeMap, sync::Arc};

#[tokio::main]
async fn main() {
    let result = async {
        let env: BTreeMap<String, String> = std::env::vars().collect();
        let account = env
            .get("LAKE_STORAGE_ACCOUNT_NAME")
            .filter(|v| !v.is_empty())
            .ok_or_else(|| {
                lake_contracts::DataError::value("LAKE_STORAGE_ACCOUNT_NAME is required")
            })?;
        let container = env
            .get("LAKE_STORAGE_CONTAINER")
            .map(String::as_str)
            .unwrap_or("lake");
        let credential: Arc<dyn azure_core::auth::TokenCredential> = Arc::new(
            azure_identity::DefaultAzureCredentialBuilder::new()
                .build()
                .map_err(lake_contracts::DataError::io)?,
        );
        let jobs = IngestionJobs {
            store: JobStore::new(
                format!("https://{account}.blob.core.windows.net/{container}"),
                credential.clone(),
            )?,
            runner: AciRunner::new(AciConfig::from_environment(&env)?, credential)?,
        };
        jobs.reconcile().await
    }
    .await;
    if let Err(error) = result {
        eprintln!("ingestion reconciliation failed: {error}");
        std::process::exit(1);
    }
}
