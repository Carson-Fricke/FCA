//! Shared harness for the lake-http integration tests.
//!
//! Every test drives the real axum [`Router`] through `tower::ServiceExt`
//! oneshot with in-memory repositories, so no network or provider is needed.

#![allow(dead_code)]

use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use lake_catalog::{
    CatalogRepository, ContractRecord, CoverageRecord, CoverageStatus, MemoryCatalogRepository,
    MemoryJobRepository,
};
use lake_contracts::{partition_path, AssetClass, DataError, DataType, Date, DateTime};
use lake_coverage::{EnsureJob, EnsureJobLauncher};
use lake_http::{create_app, ApiClock, LakeApiConfig, LakeApiState};
use lake_query::{EnsureCallback, FileUrlIssuer, QueryApplicationError};
use serde_json::Value;
use tower::ServiceExt;

/// The static bearer token every test configures.
pub const TOKEN: &str = "test-secret";

/// The pinned as-of instant used to classify open sessions.
pub fn as_of() -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 22, 12, 0, 0, 0).expect("valid datetime")
}

/// A clock provider pinned to one instant.
pub fn clock_at(instant: DateTime) -> ApiClock {
    Arc::new(move || instant)
}

/// A state with the required repositories and test defaults.
pub fn base_state(catalog: Arc<dyn CatalogRepository>) -> LakeApiState {
    LakeApiState {
        catalog,
        jobs: Arc::new(MemoryJobRepository::new()),
        listings: None,
        contracts: None,
        front_sets: None,
        sas_issuer: None,
        ensure_launcher: None,
        ensure_callback: None,
        ensure_wait_deadline_seconds: 1.0,
        bearer_token: Some(TOKEN.to_string()),
        now: clock_at(as_of()),
    }
}

/// Build the router with the standard test configuration.
pub fn build_app(state: LakeApiState) -> Router {
    create_app(
        Arc::new(state),
        LakeApiConfig {
            bearer_token: Some(TOKEN.to_string()),
            ensure_wait_deadline_seconds: 1.0,
        },
    )
}

/// Build the router with an explicit configuration.
pub fn build_app_with(state: LakeApiState, config: LakeApiConfig) -> Router {
    create_app(Arc::new(state), config)
}

/// Send one request and return its status, headers, and parsed JSON body.
pub async fn send(router: &Router, request: Request<Body>) -> (StatusCode, HeaderMap, Value) {
    let response = router.clone().oneshot(request).await.expect("oneshot");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, headers, body)
}

/// Build an authenticated GET request.
pub fn get_auth(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {TOKEN}"))
        .body(Body::empty())
        .expect("request")
}

/// Build an unauthenticated GET request.
pub fn get(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .body(Body::empty())
        .expect("request")
}

/// Build a JSON POST request with an optional bearer credential.
pub fn post_json(path: &str, token: Option<&str>, body: Value) -> Request<Body> {
    let mut builder = Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    builder.body(Body::from(body.to_string())).expect("request")
}

/// Percent-encode one query value, leaving RFC 3986 unreserved bytes.
pub fn encode_query_value(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(*byte));
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

/// Build a coverage record with canonical metadata for a complete partition.
pub fn complete_record(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    session_date: Date,
) -> CoverageRecord {
    let object_path =
        partition_path(session_date, asset_class, data_type, symbol).expect("canonical path");
    let status = CoverageStatus::Complete;
    CoverageRecord::new(
        asset_class,
        data_type,
        symbol,
        session_date,
        status,
        Some(object_path),
        12,
        3456,
        Some("checksum".to_string()),
        None,
    )
    .expect("valid record")
}

/// Build a coverage record with only a status and no object metadata.
pub fn status_record(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    session_date: Date,
    status: CoverageStatus,
) -> CoverageRecord {
    CoverageRecord::new(
        asset_class,
        data_type,
        symbol,
        session_date,
        status,
        None,
        0,
        0,
        None,
        (status == CoverageStatus::Error).then(|| "source unavailable".to_string()),
    )
    .expect("valid record")
}

/// An issuer recording every path it was asked to sign.
#[derive(Default)]
pub struct RecordingIssuer {
    paths: Mutex<Vec<String>>,
}

impl RecordingIssuer {
    /// The paths issued so far.
    pub fn paths(&self) -> Vec<String> {
        self.paths.lock().expect("lock").clone()
    }
}

impl FileUrlIssuer for RecordingIssuer {
    fn issue(&self, object_path: &str) -> Result<String, DataError> {
        self.paths
            .lock()
            .expect("lock")
            .push(object_path.to_string());
        Ok(format!(
            "https://blob.test/{object_path}?read-only-expiring"
        ))
    }
}

/// An issuer that always fails, standing in for a storage outage.
pub struct FailingIssuer;

impl FileUrlIssuer for FailingIssuer {
    fn issue(&self, _object_path: &str) -> Result<String, DataError> {
        Err(DataError::io("issuer unavailable"))
    }
}

/// An ensure launcher recording every job it received.
#[derive(Clone, Default)]
pub struct RecordingLauncher {
    jobs: Arc<Mutex<Vec<EnsureJob>>>,
}

impl RecordingLauncher {
    /// The jobs launched so far.
    pub fn jobs(&self) -> Vec<EnsureJob> {
        self.jobs.lock().expect("lock").clone()
    }

    /// The launcher seam itself.
    pub fn launcher(&self) -> EnsureJobLauncher {
        let jobs = Arc::clone(&self.jobs);
        Arc::new(move |job| {
            let jobs = Arc::clone(&jobs);
            Box::pin(async move {
                jobs.lock().expect("lock").push(job);
                Ok(())
            })
        })
    }
}

/// An ensure launcher that fills every gap by writing complete catalog rows.
pub fn filling_launcher(catalog: Arc<dyn CatalogRepository>) -> EnsureJobLauncher {
    Arc::new(move |job: EnsureJob| {
        let catalog = Arc::clone(&catalog);
        Box::pin(async move {
            for gap in &job.gaps {
                let session_date = gap.session_date();
                let object_path = partition_path(
                    session_date,
                    job.request.asset_class,
                    job.request.data_type,
                    &job.request.symbol,
                )
                .map_err(|error| DataError::value(error.to_string()))?;
                let record = CoverageRecord::new(
                    job.request.asset_class,
                    job.request.data_type,
                    job.request.symbol.clone(),
                    session_date,
                    CoverageStatus::Complete,
                    Some(object_path),
                    12,
                    3456,
                    None,
                    None,
                )
                .map_err(|error| DataError::value(error.to_string()))?;
                catalog.upsert(&record)?;
            }
            Ok(())
        })
    })
}

/// An ensure launcher that always fails.
pub fn failing_launcher(message: &'static str) -> EnsureJobLauncher {
    Arc::new(move |_job| Box::pin(async move { Err(DataError::value(message)) }))
}

/// A plain contract record for option selection tests.
pub fn contract(symbol: &str, underlying: &str, expiration: Date) -> ContractRecord {
    ContractRecord::new(
        symbol,
        underlying,
        expiration,
        lake_contracts::Decimal18::parse("500").expect("strike"),
        lake_catalog::OptionType::Call,
        100,
        None,
        None,
    )
    .expect("valid contract")
}

/// One recorded ensure callback invocation.
pub type RecordedCall = (AssetClass, DataType, String, Date, Date);

/// An ensure callback recording every request it receives.
#[derive(Clone, Default)]
pub struct RecordingCallback {
    calls: Arc<Mutex<Vec<RecordedCall>>>,
}

impl RecordingCallback {
    /// The callback invocations so far.
    pub fn calls(&self) -> Vec<RecordedCall> {
        self.calls.lock().expect("lock").clone()
    }

    /// The callback seam itself.
    pub fn callback(&self) -> EnsureCallback {
        let calls = Arc::clone(&self.calls);
        Arc::new(
            move |asset_class, data_type, symbol, start_date, end_date| {
                let calls = Arc::clone(&calls);
                Box::pin(async move {
                    calls.lock().expect("lock").push((
                        asset_class,
                        data_type,
                        symbol,
                        start_date,
                        end_date,
                    ));
                    Ok(())
                })
            },
        )
    }
}

/// An ensure callback that fills the catalog and then fails on demand.
pub fn filling_callback(
    catalog: Arc<dyn CatalogRepository>,
    records: Vec<CoverageRecord>,
) -> EnsureCallback {
    Arc::new(move |_, _, _, _, _| {
        let catalog = Arc::clone(&catalog);
        let records = records.clone();
        Box::pin(async move {
            for record in records {
                catalog.upsert(&record).map_err(|error| {
                    QueryApplicationError::new("ensure_failed", error.to_string(), None)
                })?;
            }
            Ok(())
        })
    })
}

/// A repository alias used by tests to keep both a concrete and dyn handle.
pub fn shared_catalog(
    records: impl IntoIterator<Item = CoverageRecord>,
) -> Arc<MemoryCatalogRepository> {
    Arc::new(MemoryCatalogRepository::from_records(records))
}

/// A job repository alias.
pub fn shared_jobs(
    jobs: impl IntoIterator<Item = lake_catalog::JobRecord>,
) -> Arc<MemoryJobRepository> {
    Arc::new(MemoryJobRepository::from_records(jobs))
}
