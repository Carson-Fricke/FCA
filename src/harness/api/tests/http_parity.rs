//! HTTP parity tests for the axum harness boundary.
//!
//! These mirror the Python `tests/harness/http` suites: routes, payload
//! shapes, authentication, provenance fields, and end-to-end launch behavior.
//! Everything runs in memory through the axum `tower` oneshot harness, so no
//! network access is required.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use api::http::clock::{Clock, SystemClock};
use api::http::configuration::{StrictJsonConfigurationParser, ValidatedConfiguration};
use api::http::create_app;
use api::http::launcher::{
    BacktestLauncher, BacktestRunner, InProcessLauncher, LaunchError, RunOutcome,
};
use api::http::state::AppConfig;
use api::http::store::{
    InMemoryJobRepository, InMemoryRunStore, RunCatalogRecord, RunStatus, RunStore, SasIssuer,
};
use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;

const BEARER: &str = "test-secret";

struct StubRunner {
    fail: bool,
}

impl BacktestRunner for StubRunner {
    fn run(&self, configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        if self.fail {
            return Err("StrategyLoadError: MissingStrategy".to_string());
        }
        let body = format!(
            "{{\"manifest\":{{\"run_id\":\"{}\"}},\"status\":\"completed\"}}",
            configuration.run_id
        );
        Ok(RunOutcome {
            completed: true,
            artifact_json: body.into_bytes(),
        })
    }
}

#[derive(Default, Clone)]
struct RecordingLauncher {
    jobs: Arc<Mutex<Vec<String>>>,
}

impl BacktestLauncher for RecordingLauncher {
    fn launch(&self, job: &api::http::launcher::LaunchJob) -> Result<(), LaunchError> {
        self.jobs
            .lock()
            .expect("jobs poisoned")
            .push(job.job_id.clone());
        Ok(())
    }
}

#[derive(Default)]
struct RecordingSasIssuer {
    paths: Mutex<Vec<String>>,
}

impl SasIssuer for RecordingSasIssuer {
    fn issue(&self, object_path: &str) -> String {
        self.paths
            .lock()
            .expect("paths poisoned")
            .push(object_path.to_string());
        format!("https://storage.example/{object_path}?sig=abc")
    }
}

struct TestApp {
    router: Router,
    store: Arc<InMemoryRunStore>,
}

fn build(
    token: Option<&str>,
    sas: Option<Arc<dyn SasIssuer>>,
    runner: Arc<dyn BacktestRunner>,
) -> TestApp {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let launcher = Arc::new(InProcessLauncher::new(
        store.clone(),
        jobs.clone(),
        runner,
        clock.clone(),
    ));
    let config = AppConfig {
        bearer_token: token.map(str::to_string),
        run_store: store.clone(),
        job_repository: jobs.clone(),
        launcher,
        sas_issuer: sas,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
    };
    TestApp {
        router: create_app(config).expect("app"),
        store,
    }
}

fn app_with(token: Option<&str>, runner: Arc<dyn BacktestRunner>) -> TestApp {
    build(token, None, runner)
}

fn seed(
    store: &dyn RunStore,
    run_id: &str,
    status: RunStatus,
    strategy_id: &str,
    artifacts: &[(&str, &[u8])],
) {
    let mut record = RunCatalogRecord::new(run_id, status, strategy_id);
    record.data_start = chrono::NaiveDate::from_ymd_opt(2024, 1, 2);
    record.data_end = chrono::NaiveDate::from_ymd_opt(2024, 1, 3);
    record
        .metrics
        .insert("sharpe".to_string(), "1.2345".to_string());
    record
        .metrics
        .insert("max_drawdown".to_string(), "-0.0825".to_string());
    let mut map = BTreeMap::new();
    for (name, payload) in artifacts {
        map.insert((*name).to_string(), payload.to_vec());
    }
    store.put(record, map).expect("seed");
}

async fn send(router: &Router, request: Request<Body>) -> (StatusCode, HeaderMap, Vec<u8>) {
    let response = router.clone().oneshot(request).await.expect("oneshot");
    let status = response.status();
    let headers = response.headers().clone();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes()
        .to_vec();
    (status, headers, body)
}

fn json_body(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).expect("json body")
}

fn get(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .body(Body::empty())
        .expect("request")
}

fn get_auth(path: &str, token: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .body(Body::empty())
        .expect("request")
}

fn delete_auth(path: &str, token: &str) -> Request<Body> {
    Request::builder()
        .method("DELETE")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .body(Body::empty())
        .expect("request")
}

fn post_json(path: &str, token: Option<&str>, body: Value) -> Request<Body> {
    let mut builder = Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    builder
        .body(Body::from(serde_json::to_vec(&body).expect("json")))
        .expect("request")
}

fn post_raw(path: &str, token: &str, body: &[u8]) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_vec()))
        .expect("request")
}

fn backtest_document(run_id: &str) -> Value {
    json!({
        "schema_version": 1,
        "shell": "backtest",
        "run_id": run_id,
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-19T21:00:00Z",
        "strategy_instances": [
            {"instance_id": "spy-momentum", "implementation": "x:Y", "parameters": {"threshold": "0.05"}}
        ]
    })
}

#[tokio::test]
async fn health_is_unauthenticated_and_has_probe_alias() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    for path in ["/health", "/healthz"] {
        let (status, _, body) = send(&test.router, get(path)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(json_body(&body), json!({"status": "ok"}));
    }
}

#[tokio::test]
async fn routes_reject_missing_or_invalid_credentials() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let cases: Vec<Request<Body>> = vec![
        get("/v1/backtests/runs"),
        get("/v1/backtests/runs/run-1"),
        get("/v1/backtests/runs/run-1/artifacts"),
        delete_auth("/v1/backtests/runs/run-1", "wrong"),
    ];
    for request in cases {
        let (status, headers, body) = send(&test.router, request).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(
            headers
                .get(header::WWW_AUTHENTICATE)
                .and_then(|v| v.to_str().ok()),
            Some("Bearer")
        );
        assert_eq!(
            json_body(&body),
            json!({"error": {"code": "unauthorized", "message": "a valid bearer token is required"}})
        );
    }
}

#[tokio::test]
async fn list_runs_returns_catalog_rows_sorted_by_run_id() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[
            ("fills.parquet", b"fills-bytes"),
            ("actions.parquet", b"actions-bytes"),
        ],
    );
    seed(
        test.store.as_ref(),
        "run-2",
        RunStatus::Failed,
        "mean-reversion",
        &[("backtest.json", b"{}")],
    );

    let (status, _, body) = send(&test.router, get_auth("/v1/backtests/runs", BEARER)).await;
    assert_eq!(status, StatusCode::OK);
    let runs = &json_body(&body)["runs"];
    assert_eq!(runs[0]["run_id"], "run-1");
    assert_eq!(runs[1]["run_id"], "run-2");
    assert_eq!(runs[0]["run_digest"].as_str().map(str::len), Some(64));
    assert_eq!(runs[0]["status"], "complete");
    assert_eq!(runs[0]["strategy_id"], "momentum");
    assert_eq!(runs[0]["data_start"], "2024-01-02");
    assert_eq!(runs[0]["artifact_count"], 2);
    assert_eq!(
        runs[0]["total_bytes"],
        (b"fills-bytes".len() + b"actions-bytes".len()) as u64
    );
    assert_eq!(runs[0]["metrics"]["sharpe"], "1.2345");
}

#[tokio::test]
async fn list_runs_filters_by_status_and_strategy() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[("a", b"a")],
    );
    seed(
        test.store.as_ref(),
        "run-2",
        RunStatus::Failed,
        "mean-reversion",
        &[("a", b"a")],
    );

    let (_, _, failed) = send(
        &test.router,
        get_auth("/v1/backtests/runs?status=failed", BEARER),
    )
    .await;
    assert_eq!(json_body(&failed)["runs"][0]["run_id"], "run-2");

    let (_, _, momentum) = send(
        &test.router,
        get_auth("/v1/backtests/runs?strategy_id=momentum", BEARER),
    )
    .await;
    assert_eq!(json_body(&momentum)["runs"][0]["run_id"], "run-1");

    let (_, _, both) = send(
        &test.router,
        get_auth(
            "/v1/backtests/runs?status=failed&strategy_id=momentum",
            BEARER,
        ),
    )
    .await;
    assert_eq!(json_body(&both)["runs"], json!([]));
}

#[tokio::test]
async fn list_runs_rejects_an_unknown_status_value() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let (status, _, body) = send(
        &test.router,
        get_auth("/v1/backtests/runs?status=archived", BEARER),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&body)["error"]["code"], "validation_error");
}

#[tokio::test]
async fn get_run_returns_the_catalog_row_or_404() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[("a", b"a")],
    );

    let (found_status, _, found) =
        send(&test.router, get_auth("/v1/backtests/runs/run-1", BEARER)).await;
    assert_eq!(found_status, StatusCode::OK);
    assert_eq!(json_body(&found)["run_id"], "run-1");

    let (missing_status, _, missing) =
        send(&test.router, get_auth("/v1/backtests/runs/unknown", BEARER)).await;
    assert_eq!(missing_status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&missing),
        json!({"error": {"code": "run_not_found", "message": "the requested backtest run does not exist"}})
    );
}

#[tokio::test]
async fn list_artifacts_reports_names_sizes_and_object_paths() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[
            ("fills.parquet", b"fills-bytes"),
            ("actions.parquet", b"actions-bytes"),
        ],
    );

    let (status, _, body) = send(
        &test.router,
        get_auth("/v1/backtests/runs/run-1/artifacts", BEARER),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = &json_body(&body)["artifacts"];
    assert_eq!(artifacts[0]["name"], "actions.parquet");
    assert_eq!(artifacts[1]["name"], "fills.parquet");
    assert_eq!(artifacts[0]["size"], b"actions-bytes".len() as u64);
    assert_eq!(
        artifacts[0]["object_path"],
        "backtests/run-1/actions.parquet"
    );
    assert_eq!(artifacts[0]["sas_url"], Value::Null);
}

#[tokio::test]
async fn list_artifacts_issues_scoped_sas_urls_when_configured() {
    let issuer = Arc::new(RecordingSasIssuer::default());
    let test = build(
        Some(BEARER),
        Some(issuer.clone()),
        Arc::new(StubRunner { fail: false }),
    );
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[
            ("fills.parquet", b"fills-bytes"),
            ("actions.parquet", b"actions-bytes"),
        ],
    );

    let (status, _, body) = send(
        &test.router,
        get_auth("/v1/backtests/runs/run-1/artifacts", BEARER),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = &json_body(&body)["artifacts"];
    assert_eq!(
        artifacts[1]["sas_url"],
        "https://storage.example/backtests/run-1/fills.parquet?sig=abc"
    );
    assert_eq!(
        *issuer.paths.lock().expect("paths"),
        vec![
            "backtests/run-1/actions.parquet".to_string(),
            "backtests/run-1/fills.parquet".to_string()
        ]
    );
}

#[tokio::test]
async fn download_artifact_returns_exact_payload_bytes() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[("fills.parquet", b"fills-bytes")],
    );

    let (status, headers, body) = send(
        &test.router,
        get_auth("/v1/backtests/runs/run-1/artifacts/fills.parquet", BEARER),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, b"fills-bytes");
    assert_eq!(
        headers
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok()),
        Some("application/octet-stream")
    );
}

#[tokio::test]
async fn download_artifact_rejects_unknown_runs_and_artifacts() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[("fills.parquet", b"fills-bytes")],
    );

    let (missing_run_status, _, missing_run) = send(
        &test.router,
        get_auth("/v1/backtests/runs/unknown/artifacts/fills.parquet", BEARER),
    )
    .await;
    assert_eq!(missing_run_status, StatusCode::NOT_FOUND);
    assert_eq!(json_body(&missing_run)["error"]["code"], "run_not_found");

    let (missing_artifact_status, _, missing_artifact) = send(
        &test.router,
        get_auth("/v1/backtests/runs/run-1/artifacts/missing.parquet", BEARER),
    )
    .await;
    assert_eq!(missing_artifact_status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&missing_artifact)["error"]["code"],
        "artifact_not_found"
    );
}

#[tokio::test]
async fn delete_run_removes_the_row_and_artifacts_together() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        RunStatus::Complete,
        "momentum",
        &[("fills.parquet", b"fills-bytes")],
    );
    seed(
        test.store.as_ref(),
        "run-2",
        RunStatus::Failed,
        "mean-reversion",
        &[("backtest.json", b"{}")],
    );

    let (removed_status, _, removed) = send(
        &test.router,
        delete_auth("/v1/backtests/runs/run-1", BEARER),
    )
    .await;
    assert_eq!(removed_status, StatusCode::NO_CONTENT);
    assert!(removed.is_empty());

    let (gone_status, _, _) =
        send(&test.router, get_auth("/v1/backtests/runs/run-1", BEARER)).await;
    assert_eq!(gone_status, StatusCode::NOT_FOUND);

    let (repeat_status, _, _) = send(
        &test.router,
        delete_auth("/v1/backtests/runs/run-1", BEARER),
    )
    .await;
    assert_eq!(repeat_status, StatusCode::NOT_FOUND);
    assert_eq!(
        test.store
            .list(None, None)
            .iter()
            .map(|r| r.run_id.as_str())
            .collect::<Vec<_>>(),
        vec!["run-2"]
    );
}

#[tokio::test]
async fn authenticator_fails_closed_without_a_configured_token() {
    let test = app_with(None, Arc::new(StubRunner { fail: false }));
    let (status, _, _) = send(&test.router, get_auth("/v1/backtests/runs", "any-value")).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn dev_launch_completes_and_becomes_queryable_end_to_end() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let document = backtest_document("momentum-spy-2024-01");

    let (launch_status, headers, launch_body) = send(
        &test.router,
        post_json("/v1/backtests/jobs", Some(BEARER), document),
    )
    .await;
    assert_eq!(
        launch_status,
        StatusCode::ACCEPTED,
        "{}",
        String::from_utf8_lossy(&launch_body)
    );
    assert_eq!(
        json_body(&launch_body),
        json!({"job_id": "momentum-spy-2024-01", "state": "succeeded"})
    );
    assert_eq!(
        headers.get(header::LOCATION).and_then(|v| v.to_str().ok()),
        Some("/v1/backtests/jobs/momentum-spy-2024-01")
    );

    let (job_status, _, job_body) = send(
        &test.router,
        get_auth("/v1/backtests/jobs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(job_status, StatusCode::OK);
    assert_eq!(json_body(&job_body)["state"], "succeeded");
    assert_eq!(json_body(&job_body)["error_message"], Value::Null);

    let (run_status, _, run_body) = send(
        &test.router,
        get_auth("/v1/backtests/runs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(run_status, StatusCode::OK);
    let row = json_body(&run_body);
    assert_eq!(row["status"], "complete");
    assert_eq!(row["strategy_id"], "spy-momentum");
    assert!(row["configuration_digest"].is_string());
    assert_eq!(row["artifact_count"], 1);

    let (_, _, artifact_body) = send(
        &test.router,
        get_auth(
            "/v1/backtests/runs/momentum-spy-2024-01/artifacts/backtest.json",
            BEARER,
        ),
    )
    .await;
    assert_eq!(json_body(&artifact_body)["status"], "completed");
}

#[tokio::test]
async fn failed_launch_is_recorded_on_the_job_and_leaves_no_run() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: true }));
    let document = backtest_document("momentum-spy-2024-01");

    let (launch_status, _, launch_body) = send(
        &test.router,
        post_json("/v1/backtests/jobs", Some(BEARER), document),
    )
    .await;
    assert_eq!(launch_status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&launch_body),
        json!({"job_id": "momentum-spy-2024-01", "state": "failed"})
    );

    let (_, _, job_body) = send(
        &test.router,
        get_auth("/v1/backtests/jobs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(json_body(&job_body)["state"], "failed");
    assert!(json_body(&job_body)["error_message"]
        .as_str()
        .expect("message")
        .contains("StrategyLoadError"));

    let (run_status, _, _) = send(
        &test.router,
        get_auth("/v1/backtests/runs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(run_status, StatusCode::NOT_FOUND);
    assert!(test.store.get("momentum-spy-2024-01").is_none());
}

#[tokio::test]
async fn launch_rejects_unknown_fields_and_non_object_documents() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let mut document = backtest_document("r");
    document["unexpected"] = json!(true);
    let (extra_status, _, extra) = send(
        &test.router,
        post_json("/v1/backtests/jobs", Some(BEARER), document),
    )
    .await;
    assert_eq!(extra_status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&extra)["error"]["code"], "validation_error");

    let (malformed_status, _, malformed) = send(
        &test.router,
        post_raw("/v1/backtests/jobs", BEARER, b"[1, 2]"),
    )
    .await;
    assert_eq!(malformed_status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&malformed)["error"]["code"], "validation_error");
}

#[tokio::test]
async fn launch_rejects_a_wrong_shell_document() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let (status, _, body) = send(
        &test.router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            json!({"schema_version": 1, "shell": "trader"}),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&body)["error"]["code"], "validation_error");
}

#[tokio::test]
async fn injected_launcher_keeps_asynchronous_job_semantics() {
    let launcher = Arc::new(RecordingLauncher::default());
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let config = AppConfig {
        bearer_token: Some(BEARER.to_string()),
        run_store: store,
        job_repository: jobs,
        launcher,
        sas_issuer: None,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
    };
    let router = create_app(config).expect("app");

    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("momentum-spy-2024-01"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "momentum-spy-2024-01", "state": "queued"})
    );
}

#[tokio::test]
async fn job_status_returns_404_for_an_unknown_job() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let (status, _, body) =
        send(&test.router, get_auth("/v1/backtests/jobs/unknown", BEARER)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&body),
        json!({"error": {"code": "job_not_found", "message": "the requested job does not exist"}})
    );
}

#[tokio::test]
async fn auth_check_requires_credentials() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let (status, _, body) = send(&test.router, get_auth("/v1/auth/check", BEARER)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json_body(&body), json!({"status": "ok"}));

    let (denied, _, _) = send(&test.router, get("/v1/auth/check")).await;
    assert_eq!(denied, StatusCode::UNAUTHORIZED);
}

#[test]
fn in_memory_store_helpers_are_deterministic() {
    let store = InMemoryRunStore::new();
    let mut artifacts = BTreeMap::new();
    artifacts.insert("a".to_string(), vec![1, 2]);
    let record = store
        .put(
            RunCatalogRecord::new("r", RunStatus::Complete, "s"),
            artifacts,
        )
        .expect("put");
    assert_eq!(record.total_bytes, 2);
    assert_eq!(store.list(None, Some("s")).len(), 1);
    assert_eq!(store.list(None, Some("other")).len(), 0);
}

fn temp_root(tag: &str) -> std::path::PathBuf {
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let root = std::env::temp_dir().join(format!(
        "harness-api-durable-{}-{tag}-{unique}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}

fn durable_router(root: &std::path::Path, runner: Arc<dyn BacktestRunner>) -> Router {
    let mut config = AppConfig::durable_with_runner(root.to_path_buf(), runner);
    config.bearer_token = Some(BEARER.to_string());
    create_app(config).expect("app")
}

#[tokio::test]
async fn durable_launch_survives_a_process_restart() {
    let root = temp_root("restart");
    {
        let router = durable_router(&root, Arc::new(StubRunner { fail: false }));
        let (status, _, body) = send(
            &router,
            post_json(
                "/v1/backtests/jobs",
                Some(BEARER),
                backtest_document("run-durable"),
            ),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(json_body(&body)["state"], "succeeded");
    }

    // A fresh process composition from the same root must still be able to
    // serve the run without re-executing it.
    let router = durable_router(&root, Arc::new(StubRunner { fail: true }));

    let (run_status, _, run_body) =
        send(&router, get_auth("/v1/backtests/runs/run-durable", BEARER)).await;
    assert_eq!(run_status, StatusCode::OK);
    assert_eq!(json_body(&run_body)["status"], "complete");
    assert_eq!(json_body(&run_body)["strategy_id"], "spy-momentum");

    let (job_status, _, job_body) =
        send(&router, get_auth("/v1/backtests/jobs/run-durable", BEARER)).await;
    assert_eq!(job_status, StatusCode::OK);
    assert_eq!(json_body(&job_body)["state"], "succeeded");

    let (artifact_status, _, artifact_body) = send(
        &router,
        get_auth(
            "/v1/backtests/runs/run-durable/artifacts/backtest.json",
            BEARER,
        ),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    assert_eq!(
        json_body(&artifact_body)["manifest"]["run_id"],
        "run-durable"
    );

    let _ = std::fs::remove_dir_all(&root);
}

#[tokio::test]
async fn native_engine_launch_reaches_a_terminal_state() {
    let root = temp_root("native");
    let router = durable_router(
        &root,
        Arc::new(api::http::native::NativeBacktestRunner::default()),
    );
    let document = json!({
        "schema_version": 1,
        "shell": "backtest",
        "run_id": "native-api-run",
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-04T21:00:00Z",
        "seed": 7,
        "strategy_instances": [{
            "instance_id": "spy-momentum",
            "implementation": "strategies.harness_examples.intraday_momentum:IntradayMomentumStrategy",
            "bundle_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "capital_fraction": 1.0,
            "symbols": ["SPY"],
            "parameters": {}
        }]
    });

    let (status, _, body) = send(
        &router,
        post_json("/v1/backtests/jobs", Some(BEARER), document),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "{}",
        String::from_utf8_lossy(&body)
    );
    assert_eq!(
        json_body(&body)["state"],
        "succeeded",
        "{}",
        String::from_utf8_lossy(&body)
    );

    let (run_status, _, run_body) = send(
        &router,
        get_auth("/v1/backtests/runs/native-api-run", BEARER),
    )
    .await;
    assert_eq!(run_status, StatusCode::OK);
    assert_eq!(json_body(&run_body)["status"], "complete");

    let (artifact_status, _, artifact_body) = send(
        &router,
        get_auth(
            "/v1/backtests/runs/native-api-run/artifacts/backtest.json",
            BEARER,
        ),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    assert_eq!(json_body(&artifact_body)["status"], "completed");
    assert_eq!(
        json_body(&artifact_body)["manifest"]["run_id"],
        "native-api-run"
    );

    let _ = std::fs::remove_dir_all(&root);
}
