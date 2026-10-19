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
        .put_result(
            record,
            std::collections::BTreeMap::from([("results.parquet".into(), payload.clone())]),
        )
        .unwrap();
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
