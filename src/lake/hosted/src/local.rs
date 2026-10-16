//! Local deployment composition: filesystem ports and finite child workers, sharing cloud application behavior.
use axum::{
    extract::{Path, Query},
    http::StatusCode,
    response::IntoResponse,
    Router,
};
use hmac::{Hmac, Mac};
use lake_contracts::DataError;
use lake_ingest_jobs::{IngestionJobs, LocalConfig};
use lake_query::FileUrlIssuer;
use lake_storage::ReadableBlobStore;
use sha2::Sha256;
use std::{collections::BTreeMap, sync::Arc};

pub fn local_overrides(
    env: &BTreeMap<String, String>,
    base: &str,
    bearer: &str,
) -> Result<(crate::HostedOverrides, Router), crate::HostedEnsureError> {
    let error = |e: DataError| crate::HostedEnsureError::new(e.to_string());
    let public = harness_lakeclient::LakeClientConfig::new(
        env.get("LAKE_LOCAL_PUBLIC_URL")
            .map(String::as_str)
            .unwrap_or(base),
        bearer,
    )
    .map_err(|e| crate::HostedEnsureError::new(e.to_string()))?;
    let base = public.base_url();
    let layout = lake_local::LocalLayout::from_environment(None, env)
        .map_err(|e| crate::HostedEnsureError::new(e.to_string()))?;
    layout
        .initialize()
        .map_err(|e| crate::HostedEnsureError::new(e.to_string()))?;
    let executable = env
        .get("LAKE_LOCAL_WORKER_EXECUTABLE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::env::current_exe()
                .expect("executable path")
                .with_file_name(if cfg!(windows) {
                    "lake-ingest-worker.exe"
                } else {
                    "lake-ingest-worker"
                })
        });
    let max_workers = env
        .get("LAKE_INGEST_MAX_WORKERS")
        .map(|v| v.parse())
        .transpose()
        .map_err(|e| crate::HostedEnsureError::new(format!("invalid worker limit: {e}")))?
        .unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map_or(2, std::num::NonZeroUsize::get)
                .div_ceil(2)
                .clamp(1, 8)
        });
    let timeout_seconds = env
        .get("LAKE_INGEST_TIMEOUT_SECONDS")
        .map(|v| v.parse())
        .transpose()
        .map_err(|e| crate::HostedEnsureError::new(format!("invalid worker deadline: {e}")))?
        .unwrap_or(86400);
    let jobs = Arc::new(IngestionJobs {
        store: lake_local::LocalJobStore::new(layout.root().into()),
        runner: lake_local::LocalRunner::new(LocalConfig {
            executable,
            provider_env: env
                .get("LAKE_LOCAL_PROVIDER_ENV")
                .map(std::path::PathBuf::from),
            root: layout.root().into(),
            max_workers,
            timeout_seconds,
            partition_days: env
                .get("LAKE_INGEST_PARTITION_DAYS")
                .map(|v| v.parse())
                .transpose()
                .map_err(|e| error(DataError::io(e)))?
                .unwrap_or(7),
            session_concurrency: env
                .get("LAKE_INGEST_SESSION_CONCURRENCY")
                .map(|v| v.parse())
                .transpose()
                .map_err(|e| error(DataError::io(e)))?
                .unwrap_or(2),
        })
        .map_err(error)?,
    });
    jobs.clone()
        .start_reconciler(std::time::Duration::from_millis(250));
    let catalog = Arc::new(lake_local::FileCatalog::new(
        layout.state_dir().join("catalog"),
    ));
    let read_store = Arc::new(lake_storage::LocalFilesystemStore::new(layout.objects()));
    let repository = Arc::new(lake_ingest_jobs::BlobJobRepository::new(Arc::new(
        lake_storage::LocalFilesystemStore::new(layout.root()),
    )));
    let signed = Arc::new(SignedObjects {
        store: read_store.clone(),
        base: base.into(),
        key: bearer.as_bytes().to_vec(),
    });
    let admission = crate::runtime::admission(jobs.clone());
    let options = crate::runtime::options(jobs.clone());
    let ensure_callback = crate::runtime::query_ensure(jobs);
    let route = Router::new().route(
        "/v1/objects/{*path}",
        axum::routing::get(
            move |Path(path): Path<String>, Query(query): Query<SignedQuery>| {
                let signed = signed.clone();
                async move {
                    let expires = query.expires;
                    if expires < epoch() || !signed.verify(&path, expires, &query.signature) {
                        return StatusCode::FORBIDDEN.into_response();
                    }
                    match tokio::task::spawn_blocking(move || signed.store.read(&path)).await {
                        Ok(Ok(bytes)) => (
                            [(axum::http::header::CONTENT_TYPE, "application/octet-stream")],
                            bytes,
                        )
                            .into_response(),
                        _ => StatusCode::NOT_FOUND.into_response(),
                    }
                }
            },
        ),
    );
    let issuer = Arc::new(SignedObjects {
        store: read_store.clone(),
        base: base.into(),
        key: bearer.as_bytes().to_vec(),
    });
    Ok((
        crate::HostedOverrides {
            catalog: catalog.clone(),
            jobs: repository,
            contracts: catalog,
            option_resolver: options,
            sas_issuer: issuer,
            read_store,
            ensure_callback,
            job_admission: admission,
        },
        route,
    ))
}
#[derive(serde::Deserialize)]
struct SignedQuery {
    expires: u64,
    signature: String,
}
struct SignedObjects {
    store: Arc<dyn ReadableBlobStore>,
    base: String,
    key: Vec<u8>,
}
fn epoch() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock")
        .as_secs()
}
impl SignedObjects {
    fn mac(&self, path: &str, expires: u64) -> Hmac<Sha256> {
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.key).expect("HMAC accepts any key");
        mac.update(format!("GET\n{path}\n{expires}").as_bytes());
        mac
    }
    fn verify(&self, path: &str, expires: u64, signature: &str) -> bool {
        hex::decode(signature)
            .ok()
            .is_some_and(|value| self.mac(path, expires).verify_slice(&value).is_ok())
    }
}
impl FileUrlIssuer for SignedObjects {
    fn issue(&self, path: &str) -> Result<String, DataError> {
        if !(path.starts_with("date=") || path.starts_with("reference/"))
            || path
                .split('/')
                .any(|v| v.is_empty() || v == ".." || v == ".")
        {
            return Err(DataError::value(
                "only canonical data objects may be signed",
            ));
        }
        let expires = epoch() + 900;
        let signature = hex::encode(self.mac(path, expires).finalize().into_bytes());
        Ok(format!(
            "{}/v1/objects/{path}?expires={expires}&signature={signature}",
            self.base
        ))
    }
}
