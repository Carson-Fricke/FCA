//! Real admission, partition fleet, ACA control plane, leases and request CAS over fake Azure HTTP.
use super::*;
use crate::{BudgetState, DurableJob, IngestionJobs, JobStore, WorkRequest};
use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::{HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
    Json, Router,
};
use lake_catalog::{JobProgress, JobState};
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
    etags: Mutex<BTreeMap<String, String>>,
    leases: Mutex<BTreeMap<String, String>>,
    executions: Mutex<BTreeMap<String, Value>>,
    starts: AtomicUsize,
    gets: AtomicUsize,
    get_keys: Mutex<Vec<String>>,
    revisions: AtomicUsize,
    next_lease: AtomicUsize,
    interrupt: AtomicBool,
    mismatch: AtomicBool,
    image_mismatch: AtomicBool,
    id_mismatch: AtomicBool,
    name_mismatch: AtomicBool,
    async_start: AtomicBool,
    unsafe_location: AtomicBool,
    foreign_subscription: AtomicBool,
    operation_pending: AtomicBool,
    operation_polls: AtomicUsize,
    execution_gets: AtomicUsize,
    stall_listing: AtomicBool,
    conditional_precondition: AtomicBool,
    budget_reads: AtomicUsize,
    budget_conflicts: AtomicUsize,
    budget_interrupt: AtomicBool,
    budget_barrier: Mutex<Option<Arc<tokio::sync::Barrier>>>,
    listing_entered: tokio::sync::Notify,
    listing_release: tokio::sync::Notify,
    lease_renewed: tokio::sync::Notify,
}
async fn azure(
    State(state): State<Arc<Azure>>,
    Path(path): Path<String>,
    Query(query): Query<std::collections::HashMap<String, String>>,
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
        for (name, payload) in blobs.iter().filter(|(name, _)| {
            name.starts_with(
                query
                    .get("prefix")
                    .map(String::as_str)
                    .unwrap_or("ingestion/jobs/"),
            )
        }) {
            let job: DurableJob = serde_json::from_slice(payload).unwrap();
            xml.push_str(&format!("<Blob><Name>{name}</Name><Metadata><active>{}</active><dispatched>{}</dispatched><created>{}</created></Metadata></Blob>",!job.execution_deleted,job.dispatch_started,job.record.created_at.micros()));
        }
        xml.push_str("</Blobs><NextMarker/></EnumerationResults>");
        return xml.into_response();
    }
    if let Some(key) = path.strip_prefix("blob/") {
        if method == Method::PUT && body.is_empty() && headers.get("content-length").is_none() {
            return StatusCode::LENGTH_REQUIRED.into_response();
        }
        if let Some(action) = headers
            .get("x-ms-lease-action")
            .and_then(|v| v.to_str().ok())
        {
            let mut leases = state.leases.lock().unwrap();
            if action == "acquire" {
                if !state.blobs.lock().unwrap().contains_key(key) {
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
            state.get_keys.lock().unwrap().push(key.into());
            let response = {
                let blobs = state.blobs.lock().unwrap();
                let etags = state.etags.lock().unwrap();
                match blobs.get(key) {
                    Some(bytes) => ([("etag", etags.get(key).cloned().unwrap())], bytes.clone())
                        .into_response(),
                    None => StatusCode::NOT_FOUND.into_response(),
                }
            };
            let barrier = if key.starts_with("ingestion/budgets/")
                && state.budget_reads.fetch_add(1, Ordering::SeqCst) < 2
            {
                state.budget_barrier.lock().unwrap().clone()
            } else {
                None
            };
            if let Some(barrier) = barrier {
                barrier.wait().await;
            }
            return response;
        }
        let leases = state.leases.lock().unwrap();
        let mut blobs = state.blobs.lock().unwrap();
        let mut etags = state.etags.lock().unwrap();
        if headers.contains_key("if-none-match") && blobs.contains_key(key) {
            if key.starts_with("ingestion/budgets/") {
                state.budget_conflicts.fetch_add(1, Ordering::SeqCst);
            }
            if state.conditional_precondition.load(Ordering::SeqCst) {
                return StatusCode::PRECONDITION_FAILED.into_response();
            }
            return (
                StatusCode::CONFLICT,
                [("x-ms-error-code", "BlobAlreadyExists")],
            )
                .into_response();
        }
        if let Some(expected) = headers.get("if-match").and_then(|v| v.to_str().ok()) {
            if etags.get(key).map(String::as_str) != Some(expected) {
                state.budget_conflicts.fetch_add(1, Ordering::SeqCst);
                return StatusCode::PRECONDITION_FAILED.into_response();
            }
        }
        if let Some(lease) = leases.get(key) {
            if Some(lease.as_str()) != headers.get("x-ms-lease-id").and_then(|v| v.to_str().ok()) {
                return StatusCode::PRECONDITION_FAILED.into_response();
            }
        } else if headers.contains_key("x-ms-lease-id") {
            return StatusCode::PRECONDITION_FAILED.into_response();
        }
        if key.starts_with("ingestion/jobs/") || key.starts_with("ingestion/partitions/") {
            let job: DurableJob = serde_json::from_slice(&body).unwrap();
            assert_eq!(
                headers["x-ms-meta-active"].to_str().unwrap(),
                (!job.execution_deleted).to_string()
            );
            assert_eq!(
                headers["x-ms-meta-dispatched"].to_str().unwrap(),
                job.dispatch_started.to_string()
            );
        }
        let etag = format!("\"{}\"", state.revisions.fetch_add(1, Ordering::SeqCst));
        blobs.insert(key.into(), body.to_vec());
        etags.insert(key.into(), etag.clone());
        if key.starts_with("ingestion/budgets/")
            && state.budget_interrupt.swap(false, Ordering::SeqCst)
        {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
        return (StatusCode::CREATED, [("etag", etag)]).into_response();
    }
    if path.ends_with("/executions") && method == Method::GET {
        return Json(
            json!({"value":state.executions.lock().unwrap().values().cloned().collect::<Vec<_>>()}),
        )
        .into_response();
    }
    if path.contains("/operationresults/") && method == Method::GET {
        let poll = state.operation_polls.fetch_add(1, Ordering::SeqCst);
        if poll == 0 || state.operation_pending.load(Ordering::SeqCst) {
            return (StatusCode::ACCEPTED, [("retry-after", "0")]).into_response();
        }
        let name = path.rsplit('/').next().unwrap();
        let executions = state.executions.lock().unwrap();
        let value = executions.get(name).unwrap();
        return Json(json!({"name":value["name"], "id":value["id"]})).into_response();
    }
    if path.contains("/executions/") && method == Method::GET {
        state.execution_gets.fetch_add(1, Ordering::SeqCst);
        let name = path.rsplit('/').next().unwrap();
        let Some(mut value) = state.executions.lock().unwrap().get(name).cloned() else {
            return StatusCode::NOT_FOUND.into_response();
        };
        if state.mismatch.load(Ordering::SeqCst) {
            let environment = value["properties"]["template"]["containers"][0]["env"]
                .as_array_mut()
                .unwrap();
            environment
                .iter_mut()
                .find(|entry| entry["name"] == "LAKE_INGEST_JOB_ID")
                .unwrap()["value"] = json!("other-job");
        }
        if state.image_mismatch.load(Ordering::SeqCst) {
            value["properties"]["template"]["containers"][0]["image"] = json!("other-image");
        }
        return Json(value).into_response();
    }
    if path.ends_with("/start") && method == Method::POST {
        let template: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            template["containers"][0]["resources"],
            json!({"cpu":4.0,"memory":"8Gi"})
        );
        let environment = template["containers"][0]["env"].as_array().unwrap();
        assert_eq!(
            environment
                .iter()
                .find(|entry| entry["name"] == "AZURE_CLIENT_ID")
                .unwrap()["value"],
            "worker-client"
        );
        assert!(!template.to_string().contains("coordinator-client"));
        let name = format!("ingest-{}", state.starts.fetch_add(1, Ordering::SeqCst));
        let id = format!(
            "/{}/executions/{name}",
            path.strip_suffix("/start").unwrap()
        );
        let value =
            json!({"name":name,"id":id,"properties":{"template":template,"status":"Running"}});
        state.executions.lock().unwrap().insert(name.clone(), value);
        if state.interrupt.swap(false, Ordering::SeqCst) {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
        if state.async_start.load(Ordering::SeqCst) {
            let host = headers["host"].to_str().unwrap();
            let origin = if state.unsafe_location.load(Ordering::SeqCst) {
                "http://other.invalid".to_string()
            } else {
                format!("http://{host}")
            };
            let subscription = if state.foreign_subscription.load(Ordering::SeqCst) {
                "other"
            } else {
                "sub"
            };
            let location = format!("{origin}/subscriptions/{subscription}/providers/Microsoft.App/locations/westus2/operationresults/{name}?api-version=2025-07-01");
            return (
                StatusCode::ACCEPTED,
                [("location", location), ("retry-after", "0".into())],
            )
                .into_response();
        }
        let mut base = json!({"name":name,"id":id});
        if state.id_mismatch.load(Ordering::SeqCst) {
            base["id"] = json!(format!("/subscriptions/sub/resourceGroups/group/providers/Microsoft.App/jobs/other/executions/{name}"));
        }
        if state.name_mismatch.load(Ordering::SeqCst) {
            base["name"] = json!("other-execution");
        }
        return Json(base).into_response();
    }
    if path.ends_with("/stop") && method == Method::POST {
        let name = path.rsplit('/').nth(1).unwrap();
        state.executions.lock().unwrap().get_mut(name).unwrap()["properties"]["status"] =
            json!("Stopped");
        return StatusCode::OK.into_response();
    }
    StatusCode::NOT_FOUND.into_response()
}
fn work(symbol: &str) -> Vec<WorkRequest> {
    work_range(symbol, "2024-01-18", "2024-01-18")
}
fn work_range(symbol: &str, start: &str, end: &str) -> Vec<WorkRequest> {
    vec![WorkRequest::Dataset {
        asset_class: "equity".into(),
        data_type: "trade".into(),
        symbol: symbol.into(),
        start_date: start.into(),
        end_date: end.into(),
        interval: None,
    }]
}
async fn fixture(max_workers: usize) -> (IngestionJobs, Arc<Azure>, tokio::task::JoinHandle<()>) {
    let state = Arc::new(Azure::default());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let app = Router::new()
        .route("/{*path}", any(azure))
        .layer(axum::middleware::map_response(
            |mut response: Response| async move {
                response.headers_mut().insert(
                    "date",
                    httpdate::fmt_http_date(std::time::SystemTime::now())
                        .parse()
                        .unwrap(),
                );
                response
            },
        ))
        .with_state(state.clone());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let credential: Arc<dyn TokenCredential> = Arc::new(Credential);
    let config = AcaConfig::from_environment(&BTreeMap::from([
        ("AZURE_SUBSCRIPTION_ID".into(), "sub".into()),
        ("LAKE_INGEST_RESOURCE_GROUP".into(), "group".into()),
        ("LAKE_INGEST_JOB_NAME".into(), "ingest".into()),
        (
            "LAKE_INGEST_WORKER_CLIENT_ID".into(),
            "worker-client".into(),
        ),
        ("AZURE_CLIENT_ID".into(), "coordinator-client".into()),
        (
            "LAKE_INGEST_IMAGE".into(),
            format!("registry/worker@sha256:{}", "a".repeat(64)),
        ),
        ("LAKE_INGEST_MAX_WORKERS".into(), max_workers.to_string()),
        ("LAKE_INGEST_PARTITION_DAYS".into(), "1".into()),
        ("LAKE_INGEST_TIMEOUT_SECONDS".into(), "3600".into()),
        ("LAKE_PROVIDER__API_KEY".into(), "provider-secret".into()),
        ("LAKE_API__BEARER_TOKEN".into(), "api-secret".into()),
        (
            "LAKE_PROVIDER__REQUEST__API_SECRET".into(),
            "misnamed-secret".into(),
        ),
    ]))
    .unwrap();
    let mut runner = AcaRunner::new(config, credential.clone()).unwrap();
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
async fn complete(jobs: &IngestionJobs, reference: &crate::WorkRef) {
    let owner = jobs.store.own_work(reference).await.unwrap();
    let mut partition = jobs.store.get_work(reference).await.unwrap();
    partition.record.state = JobState::Complete;
    partition.record.progress = Some(JobProgress {
        sessions_total: partition.requests.len() as u64,
        sessions_done: partition.requests.len() as u64,
        symbol: "AAPL".into(),
        session_date: "2024-01-18".into(),
        rows: 1,
        error: None,
    });
    owner.save(&partition).await.unwrap();
    owner.release().await.unwrap();
}

#[tokio::test]
async fn durable_admission_joins_conflicts_and_survives_ambiguous_post_without_replay() {
    let (mut jobs, state, server) = fixture(1).await;
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
    let mut overlap = work_range("MSFT", "2024-01-01", "2024-01-07");
    overlap.extend(work_range("MSFT", "2024-01-04", "2024-01-10"));
    assert!(jobs
        .submit("overlapping-ranges".into(), overlap)
        .await
        .is_err());
    let persisted = serde_json::to_string(&jobs.store.get("job-one").await.unwrap()).unwrap();
    assert!(
        !persisted.contains("provider-secret")
            && !persisted.contains("api-secret")
            && !persisted.contains("misnamed-secret")
    );
    jobs.runner.config.image = format!("registry/worker@sha256:{}", "b".repeat(64));
    state.interrupt.store(true, Ordering::SeqCst);
    assert!(jobs.reconcile().await.is_err());
    let partition = jobs
        .store
        .get("job-one")
        .await
        .unwrap()
        .partitions()
        .unwrap()
        .remove(0);
    assert!(
        jobs.store
            .get_work(&partition.reference())
            .await
            .unwrap()
            .dispatch_started
    );
    assert_eq!(
        state.executions.lock().unwrap().values().next().unwrap()["properties"]["template"]
            ["containers"][0]["image"],
        original_image
    );
    jobs.reconcile().await.unwrap();
    assert_eq!(
        state.starts.load(Ordering::SeqCst),
        1,
        "possibly accepted POST must never be replayed"
    );
    jobs.submit("job-two".into(), work("AAPL")).await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(
        state.starts.load(Ordering::SeqCst),
        1,
        "fleet cap spans public jobs"
    );
    complete(&jobs, &partition.reference()).await;
    jobs.reconcile().await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(state.starts.load(Ordering::SeqCst), 2);
    jobs.runner.config.image = original_image;
    assert_eq!(
        jobs.submit("job-one".into(), work("MSFT"))
            .await
            .unwrap()
            .record
            .state,
        JobState::Complete
    );
    for index in 0..4 {
        let id = format!("history-{index}");
        jobs.submit(id.clone(), work("AAPL")).await.unwrap();
        let owner = jobs.store.own(&id).await.unwrap();
        let mut finished = jobs.store.get(&id).await.unwrap();
        finished.record.state = JobState::Complete;
        finished.executor = Some(finished.reference());
        finished.dispatch_started = true;
        finished.dispatched_at = Some(finished.record.created_at.micros());
        finished.execution_deleted = true;
        owner.save(&finished).await.unwrap();
        owner.release().await.unwrap();
    }
    state.get_keys.lock().unwrap().clear();
    jobs.reconcile().await.unwrap();
    assert!(
        state
            .get_keys
            .lock()
            .unwrap()
            .iter()
            .all(|key| !key.contains("history-")),
        "settled history must not be downloaded by reconciliation"
    );
    server.abort();
}

#[tokio::test]
async fn acknowledgement_mismatch_and_lost_partition_lease_cannot_settle_work() {
    let (jobs, state, server) = fixture(1).await;
    jobs.submit("job-one".into(), work("MSFT")).await.unwrap();
    state.mismatch.store(true, Ordering::SeqCst);
    assert!(matches!(
        jobs.reconcile().await,
        Err(DataError::Protocol(_))
    ));
    let reference = jobs
        .store
        .get("job-one")
        .await
        .unwrap()
        .partitions()
        .unwrap()[0]
        .reference();
    let old = jobs.store.own_work(&reference).await.unwrap();
    state
        .leases
        .lock()
        .unwrap()
        .remove(&reference.path().unwrap());
    let current = jobs.store.own_work(&reference).await.unwrap();
    let mut partition = jobs.store.get_work(&reference).await.unwrap();
    partition.record.state = JobState::Complete;
    assert!(old.save(&partition).await.is_err());
    current.save(&partition).await.unwrap();
    current.release().await.unwrap();
    assert!(old.renew().await.is_err());
    assert_eq!(
        jobs.store.get_work(&reference).await.unwrap().record.state,
        JobState::Complete
    );
    server.abort();
}

#[tokio::test]
async fn real_minimal_start_acknowledgements_are_verified_by_exact_execution_get() {
    for asynchronous in [false, true] {
        let (jobs, state, server) = fixture(1).await;
        state.async_start.store(asynchronous, Ordering::SeqCst);
        jobs.submit("retained".into(), work("AAPL")).await.unwrap();
        jobs.reconcile().await.unwrap();
        assert_eq!(state.starts.load(Ordering::SeqCst), 1);
        assert_eq!(state.execution_gets.load(Ordering::SeqCst), 1);
        assert_eq!(
            state.operation_polls.load(Ordering::SeqCst),
            if asynchronous { 2 } else { 0 }
        );
        assert_eq!(
            jobs.store.get("retained").await.unwrap().record.state,
            JobState::Running
        );
        server.abort();
    }
}

#[tokio::test]
async fn incorrect_start_base_or_execution_template_never_causes_a_replayed_post() {
    for failure in ["id", "name", "correlation", "image"] {
        let (jobs, state, server) = fixture(1).await;
        match failure {
            "id" => state.id_mismatch.store(true, Ordering::SeqCst),
            "name" => state.name_mismatch.store(true, Ordering::SeqCst),
            "correlation" => state.mismatch.store(true, Ordering::SeqCst),
            "image" => state.image_mismatch.store(true, Ordering::SeqCst),
            _ => unreachable!(),
        }
        jobs.submit("retained".into(), work("AAPL")).await.unwrap();
        let error = jobs.reconcile().await.unwrap_err();
        assert!(
            error.to_string().contains("outcome is unknown"),
            "{failure}: {error}"
        );
        assert_eq!(
            state.execution_gets.load(Ordering::SeqCst),
            usize::from(matches!(failure, "correlation" | "image"))
        );
        state.id_mismatch.store(false, Ordering::SeqCst);
        state.name_mismatch.store(false, Ordering::SeqCst);
        state.mismatch.store(false, Ordering::SeqCst);
        state.image_mismatch.store(false, Ordering::SeqCst);
        jobs.reconcile().await.unwrap();
        assert_eq!(state.starts.load(Ordering::SeqCst), 1, "{failure}");
        server.abort();
    }
}

#[tokio::test]
async fn unsafe_or_unresolved_operation_locations_leave_one_retained_dispatch_intent() {
    for failure in ["origin", "subscription", "pending"] {
        let (jobs, state, server) = fixture(1).await;
        state.async_start.store(true, Ordering::SeqCst);
        match failure {
            "origin" => state.unsafe_location.store(true, Ordering::SeqCst),
            "subscription" => state.foreign_subscription.store(true, Ordering::SeqCst),
            "pending" => state.operation_pending.store(true, Ordering::SeqCst),
            _ => unreachable!(),
        }
        jobs.submit("retained".into(), work("AAPL")).await.unwrap();
        let error = jobs.reconcile().await.unwrap_err();
        assert!(error.to_string().contains("outcome is unknown"));
        assert_eq!(
            state.operation_polls.load(Ordering::SeqCst),
            if failure == "pending" { 32 } else { 0 }
        );
        jobs.reconcile().await.unwrap();
        assert_eq!(state.starts.load(Ordering::SeqCst), 1, "{failure}");
        server.abort();
    }
}

#[tokio::test]
async fn absent_ambiguous_dispatch_expires_without_a_new_execution() {
    let (jobs, state, server) = fixture(1).await;
    let job = jobs.submit("job-one".into(), work("MSFT")).await.unwrap();
    let partition = job.partitions().unwrap().remove(0);
    let reference = partition.reference();
    jobs.store.admit_work(partition).await.unwrap();
    let owner = jobs.store.own_work(&reference).await.unwrap();
    let mut partition = jobs.store.get_work(&reference).await.unwrap();
    partition.executor = Some(partition.reference());
    partition.dispatch_started = true;
    partition.dispatched_at = Some(job.record.created_at.micros() - 7_200_000_000);
    owner.save(&partition).await.unwrap();
    owner.release().await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(
        jobs.store.get("job-one").await.unwrap().record.state,
        JobState::Failed
    );
    assert_eq!(state.starts.load(Ordering::SeqCst), 0);
    server.abort();
}

#[tokio::test]
async fn slow_control_plane_renews_ownership_and_lost_lease_cancels_dispatch() {
    let (jobs, state, server) = fixture(1).await;
    jobs.submit("job-one".into(), work("MSFT")).await.unwrap();
    state.stall_listing.store(true, Ordering::SeqCst);
    let running = jobs.clone();
    let reconcile = tokio::spawn(async move { running.reconcile().await });
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
async fn partition_fleet_is_bounded_disjoint_complete_and_aggregates_one_public_job() {
    let (jobs, state, server) = fixture(2).await;
    let root = jobs
        .submit(
            "months".into(),
            work_range("AAPL", "2024-01-18", "2024-01-22"),
        )
        .await
        .unwrap();
    let plan = root.partitions().unwrap();
    assert_eq!(plan.len(), 5);
    let mut dates = Vec::new();
    for partition in &plan {
        assert_eq!(partition.record.job_id, "months");
        assert!(partition.partition.is_some());
        for request in &partition.requests {
            if let WorkRequest::Dataset {
                start_date,
                end_date,
                ..
            } = request
            {
                assert_eq!(start_date, end_date);
                dates.push(start_date.clone());
            }
        }
    }
    dates.sort();
    assert_eq!(
        dates,
        (18..=22)
            .map(|day| format!("2024-01-{day}"))
            .collect::<Vec<_>>()
    );
    for expected in [2, 4, 5] {
        jobs.reconcile().await.unwrap();
        assert_eq!(state.starts.load(Ordering::SeqCst), expected);
        let root = jobs.store.get("months").await.unwrap();
        assert_eq!(root.record.job_id, "months");
        assert_eq!(root.record.state, JobState::Running);
        let active: Vec<_> = plan
            .iter()
            .filter(|partition| {
                let bytes = state
                    .blobs
                    .lock()
                    .unwrap()
                    .get(&partition.reference().path().unwrap())
                    .unwrap()
                    .clone();
                let current: DurableJob = serde_json::from_slice(&bytes).unwrap();
                current.dispatch_started && !current.terminal()
            })
            .map(DurableJob::reference)
            .collect();
        assert!(active.len() <= 2);
        for reference in active {
            complete(&jobs, &reference).await;
        }
    }
    jobs.reconcile().await.unwrap();
    let outcome = jobs.store.get("months").await.unwrap();
    assert_eq!(outcome.record.state, JobState::Complete);
    assert!(outcome.execution_deleted);
    let progress = outcome.record.progress.unwrap();
    assert_eq!((progress.sessions_done, progress.sessions_total), (5, 5));
    assert_eq!(state.starts.load(Ordering::SeqCst), 5);
    let roots: Vec<_> = state
        .blobs
        .lock()
        .unwrap()
        .keys()
        .filter(|key| key.starts_with("ingestion/jobs/"))
        .cloned()
        .collect();
    assert_eq!(roots, vec!["ingestion/jobs/months.json"]);
    server.abort();
}

#[tokio::test]
async fn warm_worker_claims_an_unstarted_chunk_without_another_container_start() {
    let (jobs, state, server) = fixture(2).await;
    let root = jobs
        .submit(
            "reuse".into(),
            work_range("AAPL", "2024-01-18", "2024-01-22"),
        )
        .await
        .unwrap();
    jobs.reconcile().await.unwrap();
    let mut anchor = None;
    for part in root.partitions().unwrap() {
        let part = jobs.store.get_work(&part.reference()).await.unwrap();
        if part.dispatch_started {
            anchor = Some(part);
            break;
        }
    }
    let mut anchor = anchor.unwrap();
    let owner = jobs.store.own_work(&anchor.reference()).await.unwrap();
    anchor.record.state = JobState::Running;
    owner.save(&anchor).await.unwrap();
    owner.release().await.unwrap();
    let next = crate::claim_next(&jobs.store, &anchor, "reuse")
        .await
        .unwrap()
        .unwrap();
    let stolen = jobs.store.get_work(&next).await.unwrap();
    assert_eq!(stolen.executor, Some(anchor.reference()));
    assert!(stolen.dispatch_started);
    complete(&jobs, &next).await;
    jobs.reconcile().await.unwrap();
    assert_eq!(state.starts.load(Ordering::SeqCst), 2);
    assert_eq!(
        jobs.runner.inspect(&anchor.reference()).await.unwrap(),
        ExecutionState::Active
    );
    assert!(!jobs.store.get_work(&next).await.unwrap().execution_deleted);
    server.abort();
}

#[tokio::test]
async fn conditional_412_admission_and_dispatch_ownership_join_without_relaunch() {
    let (jobs, state, server) = fixture(1).await;
    state.conditional_precondition.store(true, Ordering::SeqCst);
    jobs.submit("conditional".into(), work("AAPL"))
        .await
        .unwrap();
    assert_eq!(
        jobs.submit("conditional".into(), work("AAPL"))
            .await
            .unwrap()
            .record
            .job_id,
        "conditional"
    );
    assert!(jobs
        .submit("conditional".into(), work("MSFT"))
        .await
        .is_err());
    jobs.reconcile().await.unwrap();
    jobs.reconcile().await.unwrap();
    assert_eq!(state.starts.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn concurrent_blob_budget_reservations_retry_cas_without_overspending() {
    let (jobs, state, server) = fixture(2).await;
    *state.budget_barrier.lock().unwrap() = Some(Arc::new(tokio::sync::Barrier::new(2)));
    let scope = "a".repeat(64);
    let (first, second) = tokio::join!(
        jobs.store.reserve_requests(&scope, 10000, 6000),
        jobs.store.reserve_requests(&scope, 10000, 6000)
    );
    let counts = [first.unwrap().count, second.unwrap().count];
    assert_eq!(counts.iter().sum::<u32>(), 10000);
    assert!(counts.contains(&6000) && counts.contains(&4000));
    assert!(state.budget_conflicts.load(Ordering::SeqCst) > 0);
    let restarted =
        JobStore::new(jobs.store.endpoint.clone(), jobs.store.credential.clone()).unwrap();
    assert_eq!(
        restarted
            .reserve_requests(&scope, 10000, 1)
            .await
            .unwrap()
            .count,
        0
    );
    let payload = state
        .blobs
        .lock()
        .unwrap()
        .get(&format!("ingestion/budgets/{scope}.json"))
        .unwrap()
        .clone();
    let persisted: BudgetState = serde_json::from_slice(&payload).unwrap();
    assert_eq!(
        persisted
            .charges
            .iter()
            .map(|charge| charge.count)
            .sum::<u32>(),
        10000
    );
    server.abort();
}

#[tokio::test]
async fn lost_reservation_acknowledgement_stays_charged_after_broker_restart() {
    let (jobs, state, server) = fixture(2).await;
    let scope = "b".repeat(64);
    state.budget_interrupt.store(true, Ordering::SeqCst);
    assert!(jobs
        .store
        .reserve_requests(&scope, 10000, 10000)
        .await
        .is_err());
    let restarted =
        JobStore::new(jobs.store.endpoint.clone(), jobs.store.credential.clone()).unwrap();
    assert_eq!(
        restarted
            .reserve_requests(&scope, 10000, 1)
            .await
            .unwrap()
            .count,
        0
    );
    server.abort();
}
