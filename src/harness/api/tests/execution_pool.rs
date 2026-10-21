//! Admission, interruption, fencing and HTTP availability at the execution boundary.
//! Replaces the former thread-specific active worker test in job_id_receipt.
use api::{
    execution::{
        backend::{Backend, Local},
        dispatch::QueuedLauncher,
        store::{Document, ExecutionStore},
        worker::Slot,
        Wake,
    },
    http::{create_app, state::AppConfig},
};
use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use mcp::store::{JobState, JobStore};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use tower::ServiceExt;

/// External Azure service fake: version conflicts and failures cross the real
/// admission, metadata backend, ownership and publication boundaries.
#[derive(Default)]
struct AzureObjects {
    blobs: std::sync::Mutex<std::collections::BTreeMap<String, (Vec<u8>, u64)>>,
    failure: std::sync::Mutex<Option<(String, lake_contracts::DataError)>>,
    steal_claim: std::sync::atomic::AtomicBool,
    ambiguous_write: std::sync::atomic::AtomicBool,
    committed_conflict: std::sync::Mutex<Option<String>>,
    blocked_read:
        std::sync::Mutex<Option<(std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>)>>,
}
impl lake_storage::AzureBlobContainer for AzureObjects {
    fn download_versioned(
        &self,
        path: &str,
    ) -> Result<(Vec<u8>, String), lake_contracts::DataError> {
        if let Some((entered, release)) = self.blocked_read.lock().unwrap().take() {
            entered.send(()).unwrap();
            release
                .recv_timeout(std::time::Duration::from_secs(2))
                .unwrap();
        }
        let mut failure = self.failure.lock().unwrap();
        if failure.as_ref().is_some_and(|(key, _)| key == path) {
            return Err(failure.take().unwrap().1);
        }
        self.blobs
            .lock()
            .unwrap()
            .get(path)
            .map(|(data, version)| (data.clone(), version.to_string()))
            .ok_or_else(|| lake_contracts::DataError::not_found(path))
    }
    fn upload_if_match(
        &self,
        path: &str,
        data: &[u8],
        etag: &str,
    ) -> Result<(), lake_contracts::DataError> {
        let mut blobs = self.blobs.lock().unwrap();
        let (current, version) = blobs.get_mut(path).unwrap();
        if path.starts_with("execution/jobs/") && self.steal_claim.swap(false, Ordering::SeqCst) {
            let mut other: Value = serde_json::from_slice(data).unwrap();
            other["owner"]["token"] = json!("another-worker");
            *current = serde_json::to_vec(&other).unwrap();
            *version += 1;
        }
        if version.to_string() != etag {
            return Err(lake_contracts::DataError::conflict("ETag changed"));
        }
        *current = data.to_vec();
        *version += 1;
        let mut committed_conflict = self.committed_conflict.lock().unwrap();
        if committed_conflict.as_deref() == Some(path) {
            committed_conflict.take();
            return Err(lake_contracts::DataError::conflict(
                "write committed; SDK retried its ETag after acknowledgement disconnected",
            ));
        }
        if path.starts_with("execution/jobs/") && self.ambiguous_write.swap(false, Ordering::SeqCst)
        {
            return Err(lake_contracts::DataError::io(
                "write committed; acknowledgement disconnected",
            ));
        }
        Ok(())
    }
    fn upload_file(
        &self,
        path: &str,
        source: &std::path::Path,
    ) -> Result<(), lake_contracts::DataError> {
        self.upload(path, &std::fs::read(source)?, true)
    }
    fn upload(
        &self,
        path: &str,
        data: &[u8],
        overwrite: bool,
    ) -> Result<(), lake_contracts::DataError> {
        let mut blobs = self.blobs.lock().unwrap();
        if !overwrite && blobs.contains_key(path) {
            return Err(lake_contracts::DataError::conflict("already exists"));
        }
        let version = blobs.get(path).map_or(1, |(_, version)| version + 1);
        blobs.insert(path.into(), (data.to_vec(), version));
        Ok(())
    }
    fn download(&self, path: &str) -> Result<Vec<u8>, lake_contracts::DataError> {
        self.download_versioned(path).map(|(data, _)| data)
    }
    fn download_many(&self, paths: &[String]) -> Result<Vec<Vec<u8>>, lake_contracts::DataError> {
        paths.iter().map(|p| self.download(p)).collect()
    }
    fn list(&self, prefix: &str) -> Result<Vec<String>, lake_contracts::DataError> {
        Ok(self
            .blobs
            .lock()
            .unwrap()
            .keys()
            .filter(|p| p.starts_with(prefix))
            .cloned()
            .collect())
    }
    fn delete(&self, path: &str) -> Result<(), lake_contracts::DataError> {
        self.blobs.lock().unwrap().remove(path);
        Ok(())
    }
    fn acquire_lease(&self, _: &str, _: u32) -> Result<String, lake_contracts::DataError> {
        panic!("metadata must not acquire a blob lease")
    }
    fn renew_lease(&self, _: &str, _: &str) -> Result<(), lake_contracts::DataError> {
        panic!("metadata must not renew a blob lease")
    }
    fn release_lease(&self, _: &str, _: &str) -> Result<(), lake_contracts::DataError> {
        panic!("metadata must not release a blob lease")
    }
    fn upload_leased(&self, _: &str, _: &[u8], _: &str) -> Result<(), lake_contracts::DataError> {
        panic!("metadata must not use leased writes")
    }
}
struct NoSignedObjects;
impl lake_storage::SasIssuer for NoSignedObjects {
    fn issue(&self, _: &str) -> Result<String, lake_contracts::DataError> {
        panic!("probe does not transfer signed objects")
    }
}
fn azure_store() -> (Arc<AzureObjects>, ExecutionStore) {
    let objects = Arc::new(AzureObjects::default());
    let store = ExecutionStore::new(Arc::new(api::execution::backend::Azure {
        container: objects.clone(),
        sas: Arc::new(NoSignedObjects),
    }));
    (objects, store)
}

#[derive(Default)]
struct LostHint(AtomicUsize);
impl Wake for LostHint {
    fn wake(&self) -> Result<(), String> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Err("capacity acknowledgement lost".into())
    }
}
fn document() -> Value {
    json!({"schema_version":1,"shell":"backtest","job_id":"retained-job","start":"2024-01-02T14:30:00Z","end":"2024-01-02T15:00:00Z","data":{"symbols":["SPY"],"subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}})
}
fn app(store: ExecutionStore, wake: Arc<LostHint>) -> axum::Router {
    let mut config = AppConfig::in_memory();
    config.bearer_token = Some("test".into());
    config.job_store = Arc::new(store);
    config.launcher = Arc::new(QueuedLauncher(wake));
    create_app(config).unwrap()
}
async fn post(app: &axum::Router, input: &Value) -> (StatusCode, Value) {
    let response = app
        .clone()
        .oneshot(
            Request::post("/v1/backtests/jobs")
                .header("Authorization", "Bearer test")
                .body(Body::from(input.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&body).unwrap())
}

#[tokio::test]
async fn lost_capacity_hint_keeps_one_queryable_execution_and_interruption_never_replays_it() {
    let directory = tempfile::tempdir().unwrap();
    let backend = Arc::new(Local {
        root: directory.path().into(),
    });
    let store = ExecutionStore::new(backend.clone());
    let wake = Arc::new(LostHint::default());
    let api = app(store.clone(), wake.clone());
    let input = document();
    let (status, receipt) = post(&api, &input).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(receipt["job_id"], "retained-job");
    assert_eq!(receipt["state"], "queued");
    assert_eq!(post(&api, &input).await.1, receipt);
    let submitted = directory.path().join("submission.json");
    std::fs::write(&submitted, input.to_string()).unwrap();
    let local = api::local::NativeBacktestSubmitter::new(Arc::new(store.clone()), wake.clone());
    let joined = mcp::tools::BacktestSubmitter::submit(
        &local,
        submitted.to_str().unwrap(),
        None,
        Some("retained-job"),
    )
    .unwrap();
    assert_eq!(joined["state"], "queued");
    assert_eq!(joined["job_id"], receipt["job_id"]);
    assert_eq!(wake.0.load(Ordering::SeqCst), 1);
    let mut changed = input.clone();
    changed["end"] = json!("2024-01-02T15:01:00Z");
    assert_eq!(post(&api, &changed).await.0, StatusCode::CONFLICT);
    let mut file_reference = input.clone();
    file_reference["job_id"] = json!("nonportable-input");
    file_reference["strategy_instances"] =
        json!([{"parameters":{"source_path":"mutable-program.rs"}}]);
    assert_eq!(
        post(&api, &file_reference).await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert!(store.try_get("nonportable-input").unwrap().is_none());
    let reader = ExecutionStore::new(Arc::new(Local {
        root: directory.path().into(),
    }));
    let owner = reader.claim("retained-job").unwrap().unwrap();
    assert!(store.claim("retained-job").unwrap().is_none());
    let running = reader.get("retained-job").unwrap();
    assert_eq!(running.state, JobState::Running);
    assert_eq!(
        owner
            .mark_running("retained-job", chrono::Utc::now())
            .unwrap(),
        running
    );
    backend
        .update("execution/jobs/retained-job.json", &mut |bytes| {
            let mut doc: Document = serde_json::from_slice(&bytes).unwrap();
            doc.owner.as_mut().unwrap().expires = chrono::Utc::now() - chrono::Duration::seconds(1);
            Ok(serde_json::to_vec(&doc).unwrap())
        })
        .unwrap();
    reader.recover_interrupted().unwrap();
    let interrupted = reader.get("retained-job").unwrap();
    assert_eq!(interrupted.state, JobState::Failed);
    assert_eq!(interrupted.accepted_input, input);
    assert!(interrupted
        .error_message
        .as_ref()
        .unwrap()
        .contains("will not be replayed"));
    assert_eq!(
        serde_json::to_value(&interrupted.progress.last().unwrap().snapshot).unwrap()["stage"],
        "interrupted"
    );
    assert!(owner
        .put_result(interrupted.clone(), Default::default())
        .is_err());
    assert!(reader.claim("retained-job").unwrap().is_none());
    assert_eq!(post(&api, &input).await.1["state"], "failed");
    assert_eq!(wake.0.load(Ordering::SeqCst), 1);
    let response = api
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[test]
fn independent_workers_share_a_bounded_fleet_and_only_one_claims_an_execution() {
    let dir = tempfile::tempdir().unwrap();
    let store = ExecutionStore::new(Arc::new(Local {
        root: dir.path().into(),
    }));
    let one = Slot::acquire(&store, 2).unwrap().unwrap();
    let two = Slot::acquire(&store, 2).unwrap().unwrap();
    assert!(Slot::acquire(&store, 2).unwrap().is_none());
    drop(one);
    assert!(Slot::acquire(&store, 2).unwrap().is_some());
    drop(two);
}

#[test]
fn startup_reservations_bound_cold_boots_and_cannot_be_consumed_twice() {
    let dir = tempfile::tempdir().unwrap();
    let store = ExecutionStore::new(Arc::new(Local {
        root: dir.path().into(),
    }));
    let first = Slot::reserve(&store, 2).unwrap().unwrap();
    let second = Slot::reserve(&store, 2).unwrap().unwrap();
    assert!(Slot::reserve(&store, 2).unwrap().is_none());
    let worker = Slot::attach(&store, first.clone()).unwrap();
    assert!(Slot::attach(&store, first.clone()).is_err());
    Slot::release_unstarted(&store, &first).unwrap();
    assert!(Slot::reserve(&store, 2).unwrap().is_none());
    Slot::release_unstarted(&store, &second).unwrap();
    assert!(Slot::reserve(&store, 2).unwrap().is_some());
    drop(worker);
}

#[tokio::test]
async fn artifact_inventory_is_metadata_and_binary_download_preserves_the_published_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let store = ExecutionStore::new(Arc::new(Local {
        root: dir.path().into(),
    }));
    let api = app(store.clone(), Arc::new(LostHint::default()));
    assert_eq!(post(&api, &document()).await.0, StatusCode::ACCEPTED);
    let owner = store.claim("retained-job").unwrap().unwrap();
    let mut record = owner.get("retained-job").unwrap();
    record.state = JobState::Succeeded;
    let payload = vec![42u8; 200_000];
    owner
        .update_progress(
            "retained-job",
            harness_contracts::progress::ExecutionProgress::stage(
                harness_contracts::progress::ExecutionStage::ComputingResults,
            ),
            chrono::Utc::now(),
        )
        .unwrap();
    let source = dir.path().join("results.parquet");
    std::fs::write(&source, &payload).unwrap();
    let finished = owner
        .put_files(
            record,
            std::collections::BTreeMap::from([("results.parquet".into(), source)]),
        )
        .unwrap();
    assert!(finished
        .progress
        .iter()
        .all(|p| p.snapshot.completed && p.finished_at.is_some()));
    let publication = finished.progress.last().unwrap();
    assert_eq!(
        publication.snapshot.stage,
        harness_contracts::progress::ExecutionStage::PublishingArtifacts
    );
    assert_eq!(publication.snapshot.bytes, payload.len() as u64);
    let list = api
        .clone()
        .oneshot(
            Request::get("/v1/jobs/retained-job/artifacts")
                .header("Authorization", "Bearer test")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(list.status(), StatusCode::OK);
    let inventory: Value =
        serde_json::from_slice(&list.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(inventory["artifacts"][0]["sizeBytes"], 200_000);
    let response = api
        .oneshot(
            Request::get("/v1/jobs/retained-job/artifacts/results.parquet")
                .header("Authorization", "Bearer test")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .as_ref(),
        payload
    );
}

struct BlockingHint {
    entered: std::sync::Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
    release: std::sync::Mutex<std::sync::mpsc::Receiver<()>>,
}
impl Wake for BlockingHint {
    fn wake(&self) -> Result<(), String> {
        let _ = self.entered.lock().unwrap().take().unwrap().send(());
        self.release
            .lock()
            .unwrap()
            .recv_timeout(std::time::Duration::from_secs(2))
            .unwrap();
        Ok(())
    }
}
#[tokio::test]
async fn synchronous_control_work_cannot_block_health_or_job_observation() {
    let dir = tempfile::tempdir().unwrap();
    let store = ExecutionStore::new(Arc::new(Local {
        root: dir.path().into(),
    }));
    let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let mut config = AppConfig::in_memory();
    config.bearer_token = Some("test".into());
    config.job_store = Arc::new(store);
    config.launcher = Arc::new(QueuedLauncher(Arc::new(BlockingHint {
        entered: std::sync::Mutex::new(Some(entered_tx)),
        release: std::sync::Mutex::new(release_rx),
    })));
    let app = create_app(config).unwrap();
    let submit = app.clone();
    let pending = tokio::spawn(async move { post(&submit, &document()).await });
    entered_rx.await.unwrap();
    let response = tokio::time::timeout(
        std::time::Duration::from_secs(1),
        app.oneshot(Request::get("/health").body(Body::empty()).unwrap()),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    release_tx.send(()).unwrap();
    assert_eq!(pending.await.unwrap().0, StatusCode::ACCEPTED);
}

#[tokio::test]
async fn renewal_failures_recover_without_replaying_and_publish_the_retained_execution() {
    use api::execution::renewal::Ownership;
    use lake_contracts::DataError;
    use lake_storage::AzureBlobContainer;
    use mcp::store::{StorageFailureKind, StoreError};
    let (objects, store) = azure_store();
    let api = app(store.clone(), Arc::new(LostHint::default()));
    assert_eq!(post(&api, &document()).await.0, StatusCode::ACCEPTED);
    let ownership = Ownership::new(Slot::acquire(&store, 2).unwrap().unwrap());
    let owned = store.claim("retained-job").unwrap().unwrap();
    ownership
        .track(owned.clone(), "retained-job".into())
        .unwrap();
    for cause in [
        DataError::conflict("busy metadata"),
        DataError::Http {
            status: 503,
            code: Some("ServerBusy".into()),
        },
        DataError::Http {
            status: 403,
            code: Some("AuthorizationPermissionMismatch".into()),
        },
    ] {
        *objects.failure.lock().unwrap() = Some(("execution/jobs/retained-job.json".into(), cause));
        assert_eq!(ownership.renew(), std::time::Duration::from_secs(1));
        assert!(ownership.check(chrono::Utc::now()).is_ok());
        assert_eq!(ownership.renew(), std::time::Duration::from_secs(10));
    }
    *objects.failure.lock().unwrap() = Some((
        "execution/jobs/retained-job.json".into(),
        DataError::Http {
            status: 403,
            code: None,
        },
    ));
    assert!(matches!(
        store.document("retained-job"),
        Err(StoreError::Storage {
            kind: StorageFailureKind::Permission,
            operation: "read_metadata",
            ..
        })
    ));
    objects.ambiguous_write.store(true, Ordering::SeqCst);
    assert_eq!(ownership.renew(), std::time::Duration::from_secs(1));
    assert_eq!(ownership.renew(), std::time::Duration::from_secs(10));
    let temp = tempfile::tempdir().unwrap();
    let file = temp.path().join("result.json");
    std::fs::write(&file, b"verified").unwrap();
    let mut record = owned.get("retained-job").unwrap();
    record.state = JobState::Succeeded;
    owned
        .put_files(
            record,
            std::collections::BTreeMap::from([("result.json".into(), file)]),
        )
        .unwrap();
    let committed = store.document("retained-job").unwrap().unwrap();
    assert_eq!(committed.record.state, JobState::Succeeded);
    assert_eq!(committed.record.job_id, "retained-job");
    assert_eq!(
        objects
            .download(&committed.artifacts["result.json"].uri)
            .unwrap(),
        b"verified"
    );
    assert!(committed
        .record
        .progress
        .iter()
        .all(|p| p.snapshot.completed));
    assert_eq!(store.ids().unwrap(), vec!["retained-job"]);
    ownership.clear("retained-job");
}

#[tokio::test]
async fn sdk_retried_committed_claims_reconcile_their_tokens_without_duplicating_capacity() {
    use api::execution::renewal::Ownership;
    let (objects, store) = azure_store();
    assert_eq!(
        post(
            &app(store.clone(), Arc::new(LostHint::default())),
            &document()
        )
        .await
        .0,
        StatusCode::ACCEPTED
    );
    *objects.committed_conflict.lock().unwrap() = Some("execution/control/fleet.json".into());
    let token = Slot::reserve(&store, 2).unwrap().unwrap();
    let fleet: Value = serde_json::from_slice(
        &lake_storage::AzureBlobContainer::download(
            objects.as_ref(),
            "execution/control/fleet.json",
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(fleet["owners"].as_array().unwrap().len(), 1);
    *objects.committed_conflict.lock().unwrap() = Some("execution/control/fleet.json".into());
    let slot = Slot::attach(&store, token.clone()).unwrap();
    assert!(Slot::attach(&store, token).is_err());
    *objects.committed_conflict.lock().unwrap() = Some("execution/jobs/retained-job.json".into());
    let owned = store.claim("retained-job").unwrap().unwrap();
    assert!(store.claim("retained-job").unwrap().is_none());
    let ownership = Ownership::new(slot);
    ownership
        .track(owned.clone(), "retained-job".into())
        .unwrap();
    ownership.renew();
    assert!(ownership.check(chrono::Utc::now()).is_ok());
    let mut record = owned.get("retained-job").unwrap();
    record.state = JobState::Succeeded;
    owned
        .put_result(
            record,
            std::collections::BTreeMap::from([("result.json".into(), b"verified".to_vec())]),
        )
        .unwrap();
    assert_eq!(
        store.get("retained-job").unwrap().state,
        JobState::Succeeded
    );
    assert_eq!(store.ids().unwrap(), vec!["retained-job"]);
    ownership.clear("retained-job");
}

#[tokio::test]
async fn conditional_claim_conflicts_do_not_grant_a_stale_worker_ownership() {
    let (objects, store) = azure_store();
    assert_eq!(
        post(
            &app(store.clone(), Arc::new(LostHint::default())),
            &document()
        )
        .await
        .0,
        StatusCode::ACCEPTED
    );
    objects.steal_claim.store(true, Ordering::SeqCst);
    assert!(store.claim("retained-job").unwrap().is_none());
    assert_eq!(
        store
            .document("retained-job")
            .unwrap()
            .unwrap()
            .owner
            .unwrap()
            .token,
        "another-worker"
    );
}

#[tokio::test]
async fn a_slow_progress_writer_cannot_block_renewal_or_overwrite_the_new_owner_deadline() {
    use api::execution::renewal::Ownership;
    let (objects, store) = azure_store();
    assert_eq!(
        post(
            &app(store.clone(), Arc::new(LostHint::default())),
            &document()
        )
        .await
        .0,
        StatusCode::ACCEPTED
    );
    let monitor = Ownership::new(Slot::acquire(&store, 2).unwrap().unwrap());
    let owned = store.claim("retained-job").unwrap().unwrap();
    monitor.track(owned, "retained-job".into()).unwrap();
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let writer = api::execution::backend::Azure {
        container: objects,
        sas: Arc::new(NoSignedObjects),
    };
    let pending = std::thread::spawn(move || {
        let mut first = true;
        writer
            .update("execution/jobs/retained-job.json", &mut |bytes| {
                if first {
                    first = false;
                    entered_tx.send(()).unwrap();
                    release_rx
                        .recv_timeout(std::time::Duration::from_secs(2))
                        .unwrap();
                }
                let mut doc: Document = serde_json::from_slice(&bytes).unwrap();
                doc.record
                    .metrics
                    .insert("progress_marker".into(), "retained".into());
                Ok(serde_json::to_vec(&doc).unwrap())
            })
            .unwrap();
    });
    entered_rx
        .recv_timeout(std::time::Duration::from_secs(1))
        .unwrap();
    monitor.renew();
    let renewed = store
        .document("retained-job")
        .unwrap()
        .unwrap()
        .owner
        .unwrap()
        .expires;
    release_tx.send(()).unwrap();
    pending.join().unwrap();
    let final_doc = store.document("retained-job").unwrap().unwrap();
    assert_eq!(final_doc.owner.unwrap().expires, renewed);
    assert_eq!(final_doc.record.metrics["progress_marker"], "retained");
}

#[tokio::test]
async fn ownership_loss_fences_publication_and_blocked_storage_cannot_extend_deadlines() {
    use api::execution::renewal::Ownership;
    let (objects, store) = azure_store();
    assert_eq!(
        post(
            &app(store.clone(), Arc::new(LostHint::default())),
            &document()
        )
        .await
        .0,
        StatusCode::ACCEPTED
    );
    let monitor = Ownership::new(Slot::acquire(&store, 2).unwrap().unwrap());
    let owned = store.claim("retained-job").unwrap().unwrap();
    monitor.track(owned.clone(), "retained-job".into()).unwrap();
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    *objects.blocked_read.lock().unwrap() = Some((entered_tx, release_rx));
    let renew = monitor.clone();
    let blocked = std::thread::spawn(move || renew.renew());
    entered_rx
        .recv_timeout(std::time::Duration::from_secs(1))
        .unwrap();
    assert!(monitor.check(chrono::Utc::now()).is_ok());
    assert!(monitor
        .check(chrono::Utc::now() + chrono::Duration::seconds(61))
        .is_err());
    release_tx.send(()).unwrap();
    blocked.join().unwrap();
    api::execution::backend::Azure {
        container: objects,
        sas: Arc::new(NoSignedObjects),
    }
    .update("execution/jobs/retained-job.json", &mut |bytes| {
        let mut doc: Document = serde_json::from_slice(&bytes).unwrap();
        doc.owner.as_mut().unwrap().token = "replacement".into();
        Ok(serde_json::to_vec(&doc).unwrap())
    })
    .unwrap();
    monitor.renew();
    assert!(monitor.check(chrono::Utc::now()).is_err());
    let mut stale = owned.get("retained-job").unwrap();
    stale.state = JobState::Failed;
    assert!(owned.put_result(stale, Default::default()).is_err());
    assert_eq!(store.get("retained-job").unwrap().state, JobState::Running);
}
