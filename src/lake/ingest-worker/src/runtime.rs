//! Provider/Azure composition confined to the ingestion worker process.
use lake_catalog::{ContractRepository, JobState};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime};
use lake_coverage::{requested_coverage, EnsureJob, EnsureSpec};
use lake_ingest_jobs::{JobOwnership, JobPersistence, JobStore, WorkRequest};
use lake_ingestion::{
    CatalogTradeReader, EnsureIngest, EnsureProviders, LeasedAtomicIngestCommitter,
    ObjectCatalogCommitter,
};
use std::{collections::BTreeMap, sync::Arc};

fn io(error: impl std::fmt::Display) -> DataError {
    DataError::Io(error.to_string())
}
fn now() -> DateTime {
    DateTime::from_micros(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_micros()
            .min(i64::MAX as u128) as i64,
    )
}
fn required(env: &BTreeMap<String, String>, name: &str) -> Result<String, DataError> {
    env.get(name)
        .filter(|s| !s.is_empty())
        .cloned()
        .ok_or_else(|| DataError::value(format!("{name} is required")))
}

pub async fn run(id: &str) -> Result<(), DataError> {
    let mut env: BTreeMap<String, String> = std::env::vars().collect();
    if env.get("HARNESS_MARKET_DATA_MODE").map(String::as_str) == Some("local") {
        if let Ok(path) = std::env::var("LAKE_LOCAL_PROVIDER_ENV") {
            for entry in dotenvy::from_path_iter(path)
                .map_err(|_| DataError::value("local provider credential file is unavailable"))?
            {
                let (key, value) = entry
                    .map_err(|_| DataError::value("invalid local provider credential file"))?;
                if key.starts_with("LAKE_PROVIDER__") {
                    env.insert(key, value);
                }
            }
        }
        let layout = lake_local::LocalLayout::from_environment(None, &env).map_err(io)?;
        layout.initialize().map_err(io)?;
        let catalog = Arc::new(lake_local::FileCatalog::new(
            layout.state_dir().join("catalog"),
        ));
        let objects = Arc::new(lake_storage::LocalFilesystemStore::new(layout.objects()));
        let deps = WorkerDependencies {
            catalog: catalog.clone(),
            contracts: catalog.clone(),
            reads: objects.clone(),
            committer: Arc::new(LeasedAtomicIngestCommitter::new(
                Arc::new(ObjectCatalogCommitter::new(objects, catalog)),
                Arc::new(lake_local::FileLeaseProvider(
                    layout.state_dir().join("publication-locks"),
                )),
            )),
        };
        run_owned(
            lake_local::LocalJobStore::new(layout.root().into()),
            id,
            env,
            None,
            deps,
        )
        .await
    } else {
        let credential: Arc<dyn azure_core::auth::TokenCredential> = Arc::new(
            crate::identity::ManagedIdentity::new(required(&env, "AZURE_CLIENT_ID")?)?,
        );
        let account = required(&env, "LAKE_STORAGE_ACCOUNT_NAME")?;
        let container = env
            .get("LAKE_STORAGE_CONTAINER")
            .cloned()
            .unwrap_or("lake".into());
        let blob_url = format!("https://{account}.blob.core.windows.net");
        let store = JobStore::new(format!("{blob_url}/{container}"), credential.clone())?;
        let deps = azure_dependencies(&env, &account, &container, &blob_url, credential.clone())?;
        run_owned(store, id, env, Some(credential), deps).await
    }
}

struct WorkerDependencies {
    catalog: Arc<dyn lake_catalog::CatalogRepository>,
    contracts: Arc<dyn ContractRepository>,
    reads: Arc<dyn lake_storage::ReadableBlobStore>,
    committer: Arc<dyn lake_ingestion::AtomicIngestCommitter>,
}
async fn run_owned<S: JobPersistence>(
    store: S,
    id: &str,
    mut env: BTreeMap<String, String>,
    credential: Option<Arc<dyn azure_core::auth::TokenCredential>>,
    deps: WorkerDependencies,
) -> Result<(), DataError> {
    let execution_started = std::time::Instant::now();
    eprintln!(
        "{}",
        serde_json::json!({"event":"worker_started","job_id":id,"at_micros":now().micros()})
    );
    let mut retries = 0;
    let owner = loop {
        match store.own(id).await {
            Ok(owner) => break owner,
            Err(DataError::Conflict(_)) if retries < 13 => {
                retries += 1;
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            }
            Err(error) => return Err(error),
        }
    };
    let mut job = store.get(id).await?;
    if job.terminal() {
        owner.release().await?;
        return Ok(());
    }
    if !job.dispatch_started {
        return Err(DataError::Conflict(
            "worker has no durable dispatch intent".into(),
        ));
    }
    job.record.state = JobState::Running;
    job.record.updated_at = now();
    owner.save(&job).await?;
    let timeout = env
        .get("LAKE_INGEST_TIMEOUT_SECONDS")
        .map(|s| s.parse::<u64>().map_err(io))
        .transpose()?
        .unwrap_or(86400);
    let owner = Arc::new(owner);
    let (progress_send, mut progress_recv) =
        tokio::sync::watch::channel(None::<lake_catalog::JobProgress>);
    let progress: lake_ingestion::EnsureProgress = Arc::new(move |total, done, coverage| {
        progress_send.send_replace(Some(lake_catalog::JobProgress {
            sessions_total: total as u64,
            sessions_done: done as u64,
            symbol: coverage.symbol.clone(),
            session_date: coverage.session_date.isoformat(),
            rows: coverage.row_count,
            error: coverage.error_message.clone(),
        }));
    });
    let requests = job.requests.clone();
    let work_owner = owner.clone();
    let worker_id = id.to_string();
    let work = async move {
        // Provider secrets never enter API configuration, ACI command arguments, or durable work.
        if let Some(credential) = credential {
            let vault = required(&env, "LAKE_PROVIDER_KEY_VAULT_URL")?;
            let token = credential
                .get_token(&["https://vault.azure.net/.default"])
                .await
                .map_err(io)?;
            let http = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .map_err(io)?;
            for (variable, secret) in [
                ("LAKE_PROVIDER__API_KEY", "alpaca-api-key"),
                ("LAKE_PROVIDER__API_SECRET", "alpaca-api-secret"),
            ] {
                let response = http
                    .get(format!(
                        "{}/secrets/{secret}?api-version=7.4",
                        vault.trim_end_matches('/')
                    ))
                    .bearer_auth(token.token.secret())
                    .send()
                    .await
                    .map_err(|_| DataError::Io("provider secret request failed".into()))?;
                if !response.status().is_success() {
                    return Err(DataError::Io(format!(
                        "provider secret access failed: {}",
                        response.status()
                    )));
                }
                let value: serde_json::Value = response
                    .json()
                    .await
                    .map_err(|_| DataError::Io("invalid provider secret response".into()))?;
                env.insert(
                    variable.into(),
                    value
                        .get("value")
                        .and_then(serde_json::Value::as_str)
                        .filter(|s| !s.is_empty())
                        .ok_or_else(|| DataError::value("empty provider secret"))?
                        .into(),
                );
            }
        }
        let settings = lake_configuration::LakeSettings::load(None, &env).map_err(io)?;
        let workers = env
            .get("LAKE_INGEST_MAX_WORKERS")
            .map(|s| s.parse::<u32>().map_err(io))
            .transpose()?
            .unwrap_or(2);
        if workers == 0
            || settings.provider.request.max_concurrency < workers
            || settings
                .provider
                .request
                .requests_per_minute
                .is_some_and(|budget| budget < workers)
        {
            return Err(DataError::value(
                "worker count exceeds the configured provider budget",
            ));
        }
        let mut config = lake_providers::ProviderConfig::new(
            env.get("LAKE_PROVIDER__BASE_URL")
                .map(String::as_str)
                .unwrap_or("https://data.alpaca.markets"),
            settings.provider.api_key.clone(),
            settings.provider.api_secret.clone(),
        );
        config.max_concurrency =
            (settings.provider.request.max_concurrency / workers).max(1) as usize;
        config.timeout_seconds = settings.provider.request.timeout_seconds;
        config.max_attempts = settings.provider.retry.max_attempts;
        config.requests_per_minute = settings
            .provider
            .request
            .requests_per_minute
            .map(|budget| (budget / workers).max(1));
        config.honor_retry_after = settings.provider.retry.honor_retry_after;
        let governor = lake_providers::RequestGovernor::new(config.requests_per_minute);
        let client =
            Arc::new(lake_providers::AlpacaClient::with_reqwest(config, governor).map_err(io)?);
        let max_sessions = env
            .get("LAKE_INGEST_SESSION_CONCURRENCY")
            .map(|v| v.parse::<usize>().map_err(io))
            .transpose()?
            .unwrap_or(1);
        let WorkerDependencies {
            catalog,
            contracts,
            reads,
            committer,
        } = deps;
        let ingest = EnsureIngest {
            providers: EnsureProviders {
                equity: Some(client.clone()),
                crypto: Some(client.clone()),
                bars: Some(client.clone()),
                options: Some(client.clone()),
            },
            contracts: Some(contracts.clone()),
            committer,
            trades: Arc::new(CatalogTradeReader::new(catalog.clone(), reads)),
            clock: Arc::new(now),
        };
        let mut result = None;
        for request in &requests {
            work_owner.renew().await?;
            match request {
                WorkRequest::Dataset {
                    asset_class,
                    data_type,
                    symbol,
                    start_date,
                    end_date,
                    interval,
                } => {
                    let spec = EnsureSpec {
                        asset_class: AssetClass::from_value(asset_class)
                            .ok_or_else(|| DataError::value("invalid asset class"))?,
                        data_type: DataType::from_value(data_type)
                            .ok_or_else(|| DataError::value("invalid data type"))?,
                        symbol: symbol.clone(),
                        start_date: Date::parse(start_date)
                            .ok_or_else(|| DataError::value("invalid start date"))?,
                        end_date: Date::parse(end_date)
                            .ok_or_else(|| DataError::value("invalid end date"))?,
                        interval: interval.clone(),
                    };
                    let catalog_for_plan = catalog.clone();
                    let plan_spec = spec.clone();
                    let gaps = tokio::task::spawn_blocking(move || {
                        requested_coverage(
                            catalog_for_plan.as_ref(),
                            None,
                            &plan_spec,
                            now(),
                            "ensure",
                        )
                        .map(|report| report.gap_plan().gaps)
                        .map_err(io)
                    })
                    .await
                    .map_err(io)??;
                    ingest
                        .run(
                            &EnsureJob {
                                job_id: worker_id.clone(),
                                request: spec,
                                gaps,
                            },
                            Some(progress.clone()),
                            max_sessions,
                        )
                        .await?;
                }
                WorkRequest::OptionSelection { selector } => {
                    let resolved = lake_ingestion::options::resolve::OptionContractResolver::new(
                        client.clone(),
                        contracts.clone(),
                    )
                    .resolve(selector)
                    .await?;
                    result = Some(serde_json::to_value(resolved).map_err(io)?);
                }
            }
        }
        Ok::<Option<serde_json::Value>, DataError>(result)
    };
    let mut work = tokio::spawn(work);
    let deadline = tokio::time::sleep(std::time::Duration::from_secs(timeout));
    tokio::pin!(deadline);
    let result = lake_ingest_jobs::while_owned(owner.as_ref(), async {
        loop {
            tokio::select! {
                result=&mut work=>break result.map_err(io).and_then(|v|v),
                _=&mut deadline=>break Err(DataError::EnsureJob("ingestion execution deadline exceeded".into())),
                changed=progress_recv.changed()=>{
                    if changed.is_ok(){job.record.progress=progress_recv.borrow_and_update().clone();job.record.updated_at=now();owner.save(&job).await?;}
                }
            }
        }
    }).await;
    work.abort();
    owner.renew().await?;
    job.record.state = if result.is_ok() {
        JobState::Complete
    } else {
        JobState::Failed
    };
    job.result = result.as_ref().ok().cloned().flatten();
    job.record.progress = progress_recv.borrow().clone();
    job.record.updated_at = now();
    job.record.error_message = result.as_ref().err().map(ToString::to_string);
    owner.save(&job).await?;
    owner.release().await?;
    eprintln!(
        "{}",
        serde_json::json!({"event":"worker_settled","job_id":id,"seconds":execution_started.elapsed().as_secs_f64(),"state":job.record.state.value(),"rows":job.record.progress.as_ref().map(|p|p.rows)})
    );
    result.map(|_| ())
}

fn azure_dependencies(
    env: &BTreeMap<String, String>,
    account: &str,
    container: &str,
    blob_url: &str,
    credential: Arc<dyn azure_core::auth::TokenCredential>,
) -> Result<WorkerDependencies, DataError> {
    let table = env
        .get("LAKE_COVERAGE_TABLE")
        .cloned()
        .unwrap_or("coverage".into());
    let entities = Arc::new(
        lake_catalog::SdkTableEntityStore::from_account_url(
            &format!("https://{account}.table.core.windows.net"),
            &table,
            credential.clone(),
        )
        .map_err(io)?,
    );
    let catalog = Arc::new(lake_catalog::AzureTableCatalogRepository::new(
        entities.clone(),
    ));
    let contracts: Arc<dyn ContractRepository> =
        Arc::new(lake_catalog::AzureTableContractRepository::new(entities));
    let blobs = Arc::new(lake_storage::AzureBlobStore::from_account_url(
        blob_url,
        container,
        credential.clone(),
    )?);
    let reads = Arc::new(lake_storage::AzureReadableBlobStore::from_account_url(
        blob_url,
        container,
        credential.clone(),
    )?);
    let leases = Arc::new(lake_storage::AzureBlobLeaseProvider::from_account_url(
        blob_url,
        container,
        credential.clone(),
        60,
        "locks",
    )?);
    let committer = Arc::new(LeasedAtomicIngestCommitter::new(
        Arc::new(ObjectCatalogCommitter::new(blobs, catalog.clone())),
        leases,
    ));
    Ok(WorkerDependencies {
        catalog,
        contracts,
        reads,
        committer,
    })
}
