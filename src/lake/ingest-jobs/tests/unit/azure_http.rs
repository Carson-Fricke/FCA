//! Real admission/reconciliation over fake Azure HTTP, including ambiguous dispatch and fencing.
use super::*;
use crate::{IngestionJobs, JobStore, WorkRequest};
use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
    Json, Router,
};
use lake_catalog::JobState;
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Mutex,
};

#[derive(Debug)]
struct Credential;
#[async_trait::async_trait]
impl TokenCredential for Credential {
    async fn get_token(&self, _: &[&str]) -> azure_core::Result<azure_core::auth::AccessToken> {
        Ok(azure_core::auth::AccessToken::new(
            "fake",
            time::OffsetDateTime::UNIX_EPOCH + time::Duration::days(30000),
        ))
    }
    async fn clear_cache(&self) -> azure_core::Result<()> {
        Ok(())
    }
}
#[derive(Default)]
struct Azure {
    blobs: Mutex<BTreeMap<String, Vec<u8>>>,
    leases: Mutex<BTreeMap<String, String>>,
    groups: Mutex<BTreeMap<String, Value>>,
    starts: AtomicUsize,
    gets: AtomicUsize,
    next_lease: AtomicUsize,
    interrupt: AtomicBool,
    mismatch: AtomicBool,
    stall_listing: AtomicBool,
    conditional_precondition: AtomicBool,
    listing_entered: tokio::sync::Notify,
    listing_release: tokio::sync::Notify,
    lease_renewed: tokio::sync::Notify,
}
async fn azure(
    State(state): State<Arc<Azure>>,
    Path(path): Path<String>,
    method: Method,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if path == "blob" && method == Method::GET {
        if state.stall_listing.load(Ordering::SeqCst) {
            state.listing_entered.notify_one();
            state.listing_release.notified().await;
        }
        let blobs = state.blobs.lock().unwrap();
        let mut xml = String::from("<EnumerationResults><Blobs>");
        for (name, payload) in blobs
            .iter()
            .filter(|(name, _)| name.starts_with("ingestion/jobs/"))
        {
            let job: crate::DurableJob = serde_json::from_slice(payload).unwrap();
            xml.push_str(&format!("<Blob><Name>{name}</Name><Metadata><active>{}</active><dispatched>{}</dispatched><created>{}</created></Metadata></Blob>",!job.group_deleted,job.dispatch_started,job.record.created_at.micros()));
        }
        xml.push_str("</Blobs><NextMarker/></EnumerationResults>");
        return xml.into_response();
    }
    if let Some(key) = path.strip_prefix("blob/") {
        // Azure requires an explicit zero length for empty blob/lease PUTs.
        if method == Method::PUT && body.is_empty() && headers.get("content-length").is_none() {
            return StatusCode::LENGTH_REQUIRED.into_response();
        }
        let mut leases = state.leases.lock().unwrap();
        let mut blobs = state.blobs.lock().unwrap();
        if let Some(action) = headers
            .get("x-ms-lease-action")
            .and_then(|v| v.to_str().ok())
        {
            if action == "acquire" {
                if !blobs.contains_key(key) {
                    return StatusCode::NOT_FOUND.into_response();
                }
                if leases.contains_key(key) {
                    return StatusCode::CONFLICT.into_response();
                }
                let id = state.next_lease.fetch_add(1, Ordering::SeqCst).to_string();
                leases.insert(key.into(), id.clone());
                return (StatusCode::CREATED, [("x-ms-lease-id", id)]).into_response();
            }
            if leases.get(key).map(String::as_str)
                != headers.get("x-ms-lease-id").and_then(|v| v.to_str().ok())
            {
                return StatusCode::PRECONDITION_FAILED.into_response();
            }
            if action == "release" {
                leases.remove(key);
            }
            if action == "renew" && key == "ingestion/control/dispatcher.lock" {
                state.lease_renewed.notify_one();
            }
            return StatusCode::OK.into_response();
        }
        if method == Method::GET {
            state.gets.fetch_add(1, Ordering::SeqCst);
            return blobs
                .get(key)
                .cloned()
                .map(|b| b.into_response())
                .unwrap_or_else(|| StatusCode::NOT_FOUND.into_response());
        }
        if headers.contains_key("if-none-match") && blobs.contains_key(key) {
            if state.conditional_precondition.load(Ordering::SeqCst) {
                return StatusCode::PRECONDITION_FAILED.into_response();
            }
            return (
                StatusCode::CONFLICT,
                [("x-ms-error-code", "BlobAlreadyExists")],
            )
                .into_response();
        }
        if let Some(lease) = leases.get(key) {
            if Some(lease.as_str()) != headers.get("x-ms-lease-id").and_then(|v| v.to_str().ok()) {
                return StatusCode::PRECONDITION_FAILED.into_response();
            }
        } else if headers.contains_key("x-ms-lease-id") {
            return StatusCode::PRECONDITION_FAILED.into_response();
        }
        if key.starts_with("ingestion/jobs/") {
            let job: crate::DurableJob = serde_json::from_slice(&body).unwrap();
            assert_eq!(
                headers["x-ms-meta-active"].to_str().unwrap(),
                (!job.group_deleted).to_string()
            );
            assert_eq!(
                headers["x-ms-meta-dispatched"].to_str().unwrap(),
                job.dispatch_started.to_string()
            );
            assert_eq!(
                headers["x-ms-meta-created"].to_str().unwrap(),
                job.record.created_at.micros().to_string()
            );
        }
        blobs.insert(key.into(), body.to_vec());
        return StatusCode::CREATED.into_response();
    }
    let name = path.rsplit('/').next().unwrap();
    let mut groups = state.groups.lock().unwrap();
    if method == Method::DELETE {
        groups.remove(name);
        return StatusCode::NO_CONTENT.into_response();
    }
    if method == Method::GET {
        return groups
            .get(name)
            .cloned()
            .map(|v| Json(v).into_response())
            .unwrap_or_else(|| StatusCode::NOT_FOUND.into_response());
    }
    let mut value: Value = serde_json::from_slice(&body).unwrap();
    value["name"] = json!(name);
    value["properties"]["instanceView"] = json!({"state":"Running"});
    state.starts.fetch_add(1, Ordering::SeqCst);
    groups.insert(name.into(), value.clone());
    if state.interrupt.swap(false, Ordering::SeqCst) {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    if state.mismatch.load(Ordering::SeqCst) {
        value["tags"]["fca-ingestion-job"] = json!("other-job");
    }
    Json(value).into_response()
}
fn work(symbol: &str) -> Vec<WorkRequest> {
    vec![WorkRequest::Dataset {
        asset_class: "equity".into(),
        data_type: "trade".into(),
        symbol: symbol.into(),
        start_date: "2024-01-18".into(),
        end_date: "2024-01-18".into(),
        interval: None,
    }]
}
async fn fixture() -> (IngestionJobs, Arc<Azure>, tokio::task::JoinHandle<()>) {
    let state = Arc::new(Azure::default());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let app = Router::new()
        .route("/{*path}", any(azure))
        .with_state(state.clone());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let credential: Arc<dyn TokenCredential> = Arc::new(Credential);
    let config = AciConfig::from_environment(&BTreeMap::from([
        ("AZURE_SUBSCRIPTION_ID".into(), "sub".into()),
        ("LAKE_INGEST_RESOURCE_GROUP".into(), "group".into()),
        ("LAKE_INGEST_LOCATION".into(), "westus2".into()),
        (
            "LAKE_INGEST_IMAGE".into(),
            format!("registry/worker@sha256:{}", "a".repeat(64)),
        ),
        ("LAKE_INGEST_IDENTITY_ID".into(), "identity".into()),
        ("LAKE_INGEST_IDENTITY_CLIENT_ID".into(), "client".into()),
        ("LAKE_INGEST_REGISTRY".into(), "registry".into()),
        ("LAKE_INGEST_MAX_WORKERS".into(), "1".into()),
        ("LAKE_INGEST_TIMEOUT_SECONDS".into(), "3600".into()),
        ("LAKE_PROVIDER__API_KEY".into(), "provider-secret".into()),
        ("LAKE_API__BEARER_TOKEN".into(), "api-secret".into()),
        (
            "LAKE_PROVIDER__REQUEST__API_SECRET".into(),
            "misnamed-secret".into(),
        ),
    ]))
    .unwrap();
    let mut runner = AciRunner::new(config, credential.clone()).unwrap();
    runner.endpoint = base.clone();
    (
        IngestionJobs {
            store: JobStore::new(format!("{base}/blob"), credential).unwrap(),
            runner,
        },
        state,
        server,
    )
}

#[tokio::test]
async fn durable_admission_joins_conflicts_and_survives_an_ambiguous_start() {
    let (mut jobs, state, server) = fixture().await;
    let original_image = jobs.runner.config.image.clone();
    let (first, second) = tokio::join!(
        jobs.submit("job-one".into(), work("MSFT")),
        jobs.submit("job-one".into(), work("MSFT"))
    );
    assert_eq!(first.unwrap().record.job_id, second.unwrap().record.job_id);
    assert!(matches!(
        jobs.submit("job-one".into(), work("AAPL")).await,
        Err(DataError::Conflict(_))
    ));
    let persisted = serde_json::to_string(&jobs.store.get("job-one").await.unwrap()).unwrap();
    assert!(
        !persisted.contains("provider-secret")
            && !persisted.contains("api-secret")
            && !persisted.contains("misnamed-secret")
    );
    jobs.runner.config.image = format!("registry/worker@sha256:{}", "b".repeat(64));
    state.interrupt.store(true, Ordering::SeqCst);
    assert!(jobs.reconcile().await.is_err());
    assert!(jobs.store.get("job-one").await.unwrap().dispatch_started);
    assert_eq!(
        state.groups.lock().unwrap().values().next().unwrap()["properties"]["containers"][0]
            ["properties"]["image"],
        original_image
    );
    jobs.reconcile().await.unwrap();
    assert_eq!(
        state.starts.load(Ordering::SeqCst),
        1,
        "unknown start must be reconciled, never replayed"
    );
    jobs.submit("job-two".into(), work("AAPL")).await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(
        state.starts.load(Ordering::SeqCst),
        1,
        "aggregate worker cap is durable"
    );
    let owner = jobs.store.own("job-one").await.unwrap();
    let mut job = jobs.store.get("job-one").await.unwrap();
    job.record.state = JobState::Complete;
    owner.save(&job).await.unwrap();
    owner.release().await.unwrap();
    jobs.reconcile().await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(state.starts.load(Ordering::SeqCst), 2);
    assert_eq!(
        jobs.submit("job-one".into(), work("MSFT"))
            .await
            .unwrap()
            .record
            .state,
        JobState::Complete
    );
    for index in 0..20 {
        let id = format!("history-{index}");
        jobs.submit(id.clone(), work("AAPL")).await.unwrap();
        let owner = jobs.store.own(&id).await.unwrap();
        let mut job = jobs.store.get(&id).await.unwrap();
        job.record.state = JobState::Complete;
        job.dispatch_started = true;
        job.dispatched_at = Some(job.record.created_at.micros());
        job.group_deleted = true;
        owner.save(&job).await.unwrap();
        owner.release().await.unwrap();
    }
    state.gets.store(0, Ordering::SeqCst);
    jobs.reconcile().await.unwrap();
    assert_eq!(
        state.gets.load(Ordering::SeqCst),
        1,
        "completed history must not be downloaded by the reconciler"
    );
    server.abort();
}

#[tokio::test]
async fn mismatched_acks_and_expired_worker_ownership_cannot_settle_jobs() {
    let (jobs, state, server) = fixture().await;
    jobs.submit("job-one".into(), work("MSFT")).await.unwrap();
    state.mismatch.store(true, Ordering::SeqCst);
    assert!(matches!(
        jobs.reconcile().await,
        Err(DataError::Conflict(_))
    ));
    let old = jobs.store.own("job-one").await.unwrap();
    state
        .leases
        .lock()
        .unwrap()
        .remove("ingestion/jobs/job-one.json");
    let current = jobs.store.own("job-one").await.unwrap();
    let mut job = jobs.store.get("job-one").await.unwrap();
    job.record.state = JobState::Complete;
    assert!(old.save(&job).await.is_err());
    current.save(&job).await.unwrap();
    current.release().await.unwrap();
    assert!(old.renew().await.is_err());
    assert_eq!(
        jobs.store.get("job-one").await.unwrap().record.state,
        JobState::Complete
    );
    server.abort();
}

#[tokio::test]
async fn failed_container_creation_has_a_bounded_durable_outcome() {
    let (jobs, state, server) = fixture().await;
    jobs.submit("job-one".into(), work("MSFT")).await.unwrap();
    let owner = jobs.store.own("job-one").await.unwrap();
    let mut job = jobs.store.get("job-one").await.unwrap();
    job.dispatch_started = true;
    job.dispatched_at = Some(job.record.created_at.micros() - 7_200_000_000);
    owner.save(&job).await.unwrap();
    owner.release().await.unwrap();
    jobs.reconcile().await.unwrap();
    let outcome = jobs.store.get("job-one").await.unwrap();
    assert_eq!(outcome.record.state, JobState::Failed);
    assert_eq!(
        state.starts.load(Ordering::SeqCst),
        0,
        "expired intent must not create a fresh group"
    );
    server.abort();
}

#[tokio::test]
async fn slow_control_plane_keeps_ownership_and_lost_lease_cancels_dispatch() {
    let (jobs, state, server) = fixture().await;
    jobs.submit("job-one".into(), work("MSFT")).await.unwrap();
    state.stall_listing.store(true, Ordering::SeqCst);
    let running_jobs = jobs.clone();
    let reconcile = tokio::spawn(async move { running_jobs.reconcile().await });
    state.listing_entered.notified().await;
    tokio::time::pause();
    tokio::time::advance(std::time::Duration::from_secs(16)).await;
    tokio::time::resume();
    tokio::time::timeout(
        std::time::Duration::from_secs(1),
        state.lease_renewed.notified(),
    )
    .await
    .unwrap();
    state
        .leases
        .lock()
        .unwrap()
        .remove("ingestion/control/dispatcher.lock");
    tokio::time::pause();
    tokio::time::advance(std::time::Duration::from_secs(16)).await;
    tokio::time::resume();
    assert!(
        tokio::time::timeout(std::time::Duration::from_secs(1), reconcile)
            .await
            .unwrap()
            .unwrap()
            .is_err()
    );
    assert_eq!(state.starts.load(Ordering::SeqCst), 0);
    state.stall_listing.store(false, Ordering::SeqCst);
    state.listing_release.notify_one();
    jobs.reconcile().await.unwrap();
    assert_eq!(state.starts.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn conditional_preconditions_join_existing_input_and_reuse_dispatch_ownership() {
    let (jobs, azure, server) = fixture().await;
    azure.conditional_precondition.store(true, Ordering::SeqCst);
    jobs.submit("conditional-412".into(), work("AAPL"))
        .await
        .unwrap();
    let joined = jobs
        .submit("conditional-412".into(), work("AAPL"))
        .await
        .unwrap();
    assert_eq!(joined.record.job_id, "conditional-412");
    assert!(jobs
        .submit("conditional-412".into(), work("MSFT"))
        .await
        .is_err());
    jobs.reconcile().await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(azure.starts.load(Ordering::SeqCst), 1);
    server.abort();
}
