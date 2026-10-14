//! HTTP parity tests for the axum harness boundary.
//!
//! These mirror the Python `tests/harness/http` suites: routes, payload
//! shapes, authentication, provenance fields, and end-to-end launch behavior.
//! Everything runs in memory through the axum `tower` oneshot harness, so no
//! network access is required.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use api::http::clock::{Clock, SystemClock};
use api::http::configuration::{StrictJsonConfigurationParser, ValidatedConfiguration};
use api::http::create_app;
use api::http::launcher::{
    BacktestLauncher, BacktestRunner, InProcessLauncher, LaunchError, RunOutcome,
};
use api::http::state::AppConfig;
use api::http::store::{
    artifact_digest, Admission, FileSystemJobStore, InMemoryJobStore, JobKind, JobRecord, JobState,
    JobStore, SasIssuer, StoreError, StoredJob,
};
use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use chrono::{TimeZone, Utc};
use harness_lakeclient::testing::{
    bars_stream, quote_stream, trade_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer,
    FakeQuoteRow, FakeTradeRow,
};
use harness_lakeclient::{BlockingLakeClient, LakeClientConfig};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;

const BEARER: &str = "test-secret";
const LAKE_SESSION_COUNT: usize = 3;
const LAKE_TOKEN: &str = "http-parity-lake-token";

/// A small real route fixture: three XNYS sessions of a rising quote ladder
/// plus one trade per session, so the momentum strategy enters and liquidates
/// and the research models observe trades.
fn spy_quote_rows() -> Vec<FakeQuoteRow> {
    let mut rows = Vec::new();
    for day in 0..LAKE_SESSION_COUNT {
        let open = Utc
            .with_ymd_and_hms(2024, 1, 2 + day as u32, 13, 30, 0)
            .unwrap();
        for step in 0..12i64 {
            let mid = 100.00 + 0.02 * f64::from(step as i32);
            rows.push(FakeQuoteRow::new(
                open.timestamp_micros() * 1_000 + (32 * 60 * step) * 1_000_000_000,
                &format!("{:.2}", mid - 0.01),
                &format!("{:.2}", mid + 0.01),
                100,
                100,
                "XNYS",
            ));
        }
    }
    rows
}

fn spy_trade_rows() -> Vec<FakeTradeRow> {
    let mut rows = Vec::new();
    for day in 0..LAKE_SESSION_COUNT {
        let open = Utc
            .with_ymd_and_hms(2024, 1, 2 + day as u32, 13, 30, 0)
            .unwrap();
        rows.push(FakeTradeRow::new(
            open.timestamp_micros() * 1_000 + 60 * 1_000_000_000,
            day as u64 + 1,
            "100.02",
            20,
            "XNYS",
        ));
    }
    rows
}

/// Three one-minute SPY bars inside the study run window.
fn spy_bar_rows() -> Vec<FakeBarRow> {
    let open = Utc.with_ymd_and_hms(2024, 1, 2, 14, 30, 0).unwrap();
    let base = open.timestamp_micros() * 1_000;
    vec![
        FakeBarRow::new(base, "100.00", "100.50", "99.50", "100.25", "1000.00"),
        FakeBarRow::new(
            base + 60 * 1_000_000_000,
            "100.25",
            "101.00",
            "100.00",
            "100.75",
            "1200.00",
        ),
        FakeBarRow::new(
            base + 120 * 1_000_000_000,
            "100.75",
            "101.25",
            "100.50",
            "101.00",
            "800.00",
        ),
    ]
}

fn spy_lake() -> FakeLakeServer {
    FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([
            ("quote".to_string(), quote_stream("SPY", &spy_quote_rows())),
            ("trade".to_string(), trade_stream("SPY", &spy_trade_rows())),
            ("bars".to_string(), bars_stream("SPY", &spy_bar_rows())),
        ]),
        ..FakeLakeConfig::new(LAKE_TOKEN)
    })
}

fn lake_client(server: &FakeLakeServer) -> Arc<BlockingLakeClient> {
    Arc::new(
        BlockingLakeClient::new(
            LakeClientConfig::new(server.base_url(), LAKE_TOKEN).expect("client config"),
        )
        .expect("blocking client"),
    )
}

struct StubRunner {
    fail: bool,
}

impl BacktestRunner for StubRunner {
    fn run(
        &self,
        configuration: &ValidatedConfiguration,
        _progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        if self.fail {
            return Err("StrategyLoadError: MissingStrategy".to_string());
        }
        let body = format!(
            "{{\"manifest\":{{\"job_id\":\"{}\"}},\"status\":\"completed\"}}",
            configuration.job_id
        );
        let mut artifacts = BTreeMap::new();
        artifacts.insert("backtest.json".to_string(), body.into_bytes());
        Ok(RunOutcome {
            completed: true,
            artifacts,
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
    store: Arc<InMemoryJobStore>,
}

fn build(
    token: Option<&str>,
    sas: Option<Arc<dyn SasIssuer>>,
    runner: Arc<dyn BacktestRunner>,
) -> TestApp {
    let store = Arc::new(InMemoryJobStore::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let launcher = Arc::new(InProcessLauncher::new(store.clone(), runner, clock.clone()));
    let config = AppConfig {
        bearer_token: token.map(str::to_string),
        job_store: store.clone(),
        launcher,
        screener_launcher: Arc::new(api::http::screener::InProcessScreenLauncher::new(
            store.clone(),
            Arc::new(api::http::screener::UnavailableScreeningRunner),
            clock.clone(),
        )),
        sas_issuer: sas,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
        mcp_server: None,
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
    store: &dyn JobStore,
    job_id: &str,
    state: JobState,
    strategy_id: &str,
    artifacts: &[(&str, &[u8])],
) {
    let now = Utc::now();
    let mut record = JobRecord::new(job_id, JobKind::Backtest, "seed-input", now);
    record.strategy_id = strategy_id.to_string();
    record.data_start = chrono::NaiveDate::from_ymd_opt(2024, 1, 2);
    record.data_end = chrono::NaiveDate::from_ymd_opt(2024, 1, 3);
    record
        .metrics
        .insert("sharpe".to_string(), "1.2345".to_string());
    record
        .metrics
        .insert("max_drawdown".to_string(), "-0.0825".to_string());
    store.admit(record).expect("seed admit");
    let mut record = store.get(job_id).expect("seeded job");
    record.state = state;
    let mut map = BTreeMap::new();
    for (name, payload) in artifacts {
        map.insert((*name).to_string(), payload.to_vec());
    }
    store.put_result(record, map).expect("seed");
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

fn backtest_document(job_id: &str) -> Value {
    json!({
        "schema_version": 1,
        "shell": "backtest",
        "job_id": job_id,
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-19T21:00:00Z",
        "data": {"symbols": ["SPY"],
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]},
        "strategy_instances": [
            {"instance_id": "spy-momentum", "implementation": "x:Y", "bundle_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
             "capital_fraction": "1", "symbols": ["SPY"], "parameters": {"threshold": "0.05"}}
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
        get("/v1/jobs"),
        get("/v1/jobs/run-1"),
        get("/v1/jobs/run-1/artifacts"),
        delete_auth("/v1/jobs/run-1", "wrong"),
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
async fn list_jobs_returns_records_sorted_by_job_id() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "job-1",
        JobState::Succeeded,
        "momentum",
        &[
            ("fills.parquet", b"fills-bytes"),
            ("actions.parquet", b"actions-bytes"),
        ],
    );
    seed(
        test.store.as_ref(),
        "job-2",
        JobState::Failed,
        "mean-reversion",
        &[("backtest.json", b"{}")],
    );

    let (status, _, body) = send(&test.router, get_auth("/v1/jobs", BEARER)).await;
    assert_eq!(status, StatusCode::OK);
    let jobs = &json_body(&body)["jobs"];
    assert_eq!(jobs[0]["job_id"], "job-1");
    assert_eq!(jobs[1]["job_id"], "job-2");
    assert_eq!(jobs[0]["run_digest"].as_str().map(str::len), Some(64));
    assert_eq!(jobs[0]["state"], "succeeded");
    assert_eq!(jobs[0]["strategy_id"], "momentum");
    assert_eq!(jobs[0]["data_start"], "2024-01-02");
    assert_eq!(jobs[0]["artifact_count"], 2);
    assert_eq!(
        jobs[0]["total_bytes"],
        (b"fills-bytes".len() + b"actions-bytes".len()) as u64
    );
    assert_eq!(jobs[0]["metrics"]["sharpe"], "1.2345");
}

#[tokio::test]
async fn list_jobs_filters_by_state_kind_and_strategy() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "job-1",
        JobState::Succeeded,
        "momentum",
        &[("a", b"a")],
    );
    seed(
        test.store.as_ref(),
        "job-2",
        JobState::Failed,
        "mean-reversion",
        &[("a", b"a")],
    );

    let (_, _, failed) = send(&test.router, get_auth("/v1/jobs?state=failed", BEARER)).await;
    assert_eq!(json_body(&failed)["jobs"][0]["job_id"], "job-2");

    let (_, _, momentum) = send(
        &test.router,
        get_auth("/v1/jobs?strategy_id=momentum", BEARER),
    )
    .await;
    assert_eq!(json_body(&momentum)["jobs"][0]["job_id"], "job-1");

    let (_, _, both) = send(
        &test.router,
        get_auth("/v1/jobs?state=failed&strategy_id=momentum", BEARER),
    )
    .await;
    assert_eq!(json_body(&both)["jobs"], json!([]));

    let (_, _, backtests) = send(&test.router, get_auth("/v1/jobs?kind=backtest", BEARER)).await;
    assert_eq!(json_body(&backtests)["jobs"].as_array().unwrap().len(), 2);

    let (_, _, studies) = send(&test.router, get_auth("/v1/jobs?kind=study", BEARER)).await;
    assert_eq!(json_body(&studies)["jobs"], json!([]));
}

#[tokio::test]
async fn list_jobs_rejects_an_unknown_state_value() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    let (status, _, body) = send(&test.router, get_auth("/v1/jobs?state=archived", BEARER)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&body)["error"]["code"], "validation_error");

    let (kind_status, _, kind_body) =
        send(&test.router, get_auth("/v1/jobs?kind=sweep", BEARER)).await;
    assert_eq!(kind_status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&kind_body)["error"]["code"], "validation_error");
}

#[tokio::test]
async fn get_job_returns_the_record_or_404() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "job-1",
        JobState::Succeeded,
        "momentum",
        &[("a", b"a")],
    );

    let (found_status, _, found) = send(&test.router, get_auth("/v1/jobs/job-1", BEARER)).await;
    assert_eq!(found_status, StatusCode::OK);
    assert_eq!(json_body(&found)["job_id"], "job-1");

    let (missing_status, _, missing) =
        send(&test.router, get_auth("/v1/jobs/unknown", BEARER)).await;
    assert_eq!(missing_status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&missing),
        json!({"error": {"code": "job_not_found", "message": "the requested job does not exist"}})
    );
}

#[tokio::test]
async fn list_artifacts_reports_the_reproducibility_manifest_shape() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        JobState::Succeeded,
        "momentum",
        &[
            ("fills.parquet", b"fills-bytes"),
            ("actions.parquet", b"actions-bytes"),
        ],
    );

    let (status, _, body) = send(&test.router, get_auth("/v1/jobs/run-1/artifacts", BEARER)).await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = &json_body(&body)["artifacts"];
    assert_eq!(artifacts[0]["name"], "actions.parquet");
    assert_eq!(artifacts[1]["name"], "fills.parquet");
    assert_eq!(artifacts[0]["sizeBytes"], b"actions-bytes".len() as u64);
    let uri = artifacts[0]["uri"].as_str().expect("artifact uri");
    assert_eq!(uri, "jobs/run-1/actions.parquet");
    let derived_name = uri.strip_prefix("jobs/run-1/").expect("job prefix");
    assert_eq!(derived_name, "actions.parquet");
    assert_eq!(artifacts[0]["sha256"], artifact_digest(b"actions-bytes"));
    assert_eq!(artifacts[0]["sas_url"], Value::Null);
    assert!(artifacts[0].get("object_path").is_none());
    assert!(artifacts[0].get("size").is_none());

    let (round_status, _, round_body) = send(
        &test.router,
        get_auth(&format!("/v1/jobs/run-1/artifacts/{derived_name}"), BEARER),
    )
    .await;
    assert_eq!(round_status, StatusCode::OK);
    assert_eq!(round_body, b"actions-bytes");
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
        JobState::Succeeded,
        "momentum",
        &[
            ("fills.parquet", b"fills-bytes"),
            ("actions.parquet", b"actions-bytes"),
        ],
    );

    let (status, _, body) = send(&test.router, get_auth("/v1/jobs/run-1/artifacts", BEARER)).await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = &json_body(&body)["artifacts"];
    assert_eq!(
        artifacts[1]["sas_url"],
        "https://storage.example/jobs/run-1/fills.parquet?sig=abc"
    );
    assert_eq!(
        *issuer.paths.lock().expect("paths"),
        vec![
            "jobs/run-1/actions.parquet".to_string(),
            "jobs/run-1/fills.parquet".to_string()
        ]
    );
}

#[tokio::test]
async fn download_artifact_returns_exact_payload_bytes() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "run-1",
        JobState::Succeeded,
        "momentum",
        &[("fills.parquet", b"fills-bytes")],
    );

    let (status, headers, body) = send(
        &test.router,
        get_auth("/v1/jobs/run-1/artifacts/fills.parquet", BEARER),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, b"fills-bytes");
    let (json_status, _, json_body) = send(
        &test.router,
        get_auth(
            "/v1/jobs/run-1/artifacts/fills.parquet?representation=json",
            BEARER,
        ),
    )
    .await;
    assert_eq!(json_status, StatusCode::OK);
    let document: Value = serde_json::from_slice(&json_body).unwrap();
    assert_eq!(document["job_id"], "run-1");
    assert_eq!(document["name"], "fills.parquet");
    assert_eq!(document["encoding"], "utf-8");
    assert_eq!(document["content"], "fills-bytes");
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
        JobState::Succeeded,
        "momentum",
        &[("fills.parquet", b"fills-bytes")],
    );

    let (missing_run_status, _, missing_run) = send(
        &test.router,
        get_auth("/v1/jobs/unknown/artifacts/fills.parquet", BEARER),
    )
    .await;
    assert_eq!(missing_run_status, StatusCode::NOT_FOUND);
    assert_eq!(json_body(&missing_run)["error"]["code"], "job_not_found");

    let (missing_artifact_status, _, missing_artifact) = send(
        &test.router,
        get_auth("/v1/jobs/run-1/artifacts/missing.parquet", BEARER),
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
        JobState::Succeeded,
        "momentum",
        &[("fills.parquet", b"fills-bytes")],
    );
    seed(
        test.store.as_ref(),
        "run-2",
        JobState::Failed,
        "mean-reversion",
        &[("backtest.json", b"{}")],
    );

    let (removed_status, _, removed) =
        send(&test.router, delete_auth("/v1/jobs/run-1", BEARER)).await;
    assert_eq!(removed_status, StatusCode::NO_CONTENT);
    assert!(removed.is_empty());

    let (gone_status, _, _) = send(&test.router, get_auth("/v1/jobs/run-1", BEARER)).await;
    assert_eq!(gone_status, StatusCode::NOT_FOUND);

    let (repeat_status, _, _) = send(&test.router, delete_auth("/v1/jobs/run-1", BEARER)).await;
    assert_eq!(repeat_status, StatusCode::NOT_FOUND);
    assert_eq!(
        test.store
            .list(None, None, None)
            .iter()
            .map(|r| r.job_id.as_str())
            .collect::<Vec<_>>(),
        vec!["run-2"]
    );
}

#[tokio::test]
async fn authenticator_fails_closed_without_a_configured_token() {
    let test = app_with(None, Arc::new(StubRunner { fail: false }));
    let (status, _, _) = send(&test.router, get_auth("/v1/jobs", "any-value")).await;
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
        Some("/v1/jobs/momentum-spy-2024-01")
    );

    let (job_status, _, job_body) = send(
        &test.router,
        get_auth("/v1/jobs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(job_status, StatusCode::OK);
    assert_eq!(json_body(&job_body)["state"], "succeeded");
    assert_eq!(json_body(&job_body)["error_message"], Value::Null);

    let (run_status, _, run_body) = send(
        &test.router,
        get_auth("/v1/jobs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(run_status, StatusCode::OK);
    let row = json_body(&run_body);
    assert_eq!(row["state"], "succeeded");
    assert_eq!(row["strategy_id"], "spy-momentum");
    assert!(row["configuration_digest"].is_string());
    assert_eq!(row["artifact_count"], 1);

    let (_, _, artifact_body) = send(
        &test.router,
        get_auth(
            "/v1/jobs/momentum-spy-2024-01/artifacts/backtest.json",
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
        get_auth("/v1/jobs/momentum-spy-2024-01", BEARER),
    )
    .await;
    assert_eq!(json_body(&job_body)["state"], "failed");
    assert!(json_body(&job_body)["error_message"]
        .as_str()
        .expect("message")
        .contains("StrategyLoadError"));

    let stored = test
        .store
        .fetch("momentum-spy-2024-01")
        .expect("failed job record");
    assert_eq!(stored.record.state, JobState::Failed);
    assert_eq!(
        stored.record.artifact_count, 0,
        "a failed launch publishes no synthetic artifact"
    );
    assert!(stored.artifacts.is_empty());

    let (artifact_status, _, artifact_body) = send(
        &test.router,
        get_auth("/v1/jobs/momentum-spy-2024-01/artifacts", BEARER),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    assert_eq!(
        json_body(&artifact_body),
        json!({"job_id": "momentum-spy-2024-01", "artifacts": []})
    );
}

struct FailingLauncher;

impl BacktestLauncher for FailingLauncher {
    fn launch(&self, _job: &api::http::launcher::LaunchJob) -> Result<(), LaunchError> {
        Err(LaunchError::Engine(
            "the backtest worker could not be started".to_string(),
        ))
    }
}

#[tokio::test]
async fn launch_failure_publishes_failed_metadata_without_artifacts() {
    let store = Arc::new(InMemoryJobStore::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let config = AppConfig {
        bearer_token: Some(BEARER.to_string()),
        job_store: store.clone(),
        launcher: Arc::new(FailingLauncher),
        screener_launcher: Arc::new(api::http::screener::InProcessScreenLauncher::new(
            store.clone(),
            Arc::new(api::http::screener::UnavailableScreeningRunner),
            clock.clone(),
        )),
        sas_issuer: None,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
        mcp_server: None,
    };
    let router = create_app(config).expect("app");

    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("job-launch-failure"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "job-launch-failure", "state": "failed"})
    );

    let stored = store.fetch("job-launch-failure").expect("failed record");
    assert_eq!(stored.record.state, JobState::Failed);
    assert_eq!(stored.record.artifact_count, 0);
    assert!(stored.artifacts.is_empty());
    assert!(stored
        .record
        .error_message
        .as_deref()
        .expect("message")
        .contains("could not be started"));

    let (list_status, _, list_body) = send(
        &router,
        get_auth("/v1/jobs/job-launch-failure/artifacts", BEARER),
    )
    .await;
    assert_eq!(list_status, StatusCode::OK);
    assert_eq!(
        json_body(&list_body),
        json!({"job_id": "job-launch-failure", "artifacts": []})
    );
}

struct EmptyRunner;

impl BacktestRunner for EmptyRunner {
    fn run(
        &self,
        _configuration: &ValidatedConfiguration,
        _progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        Ok(RunOutcome {
            completed: true,
            artifacts: BTreeMap::new(),
        })
    }
}

#[tokio::test]
async fn successful_outcome_without_artifacts_is_not_published_as_succeeded() {
    let test = app_with(Some(BEARER), Arc::new(EmptyRunner));
    let (status, _, body) = send(
        &test.router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("job-empty-success"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "job-empty-success", "state": "failed"})
    );
    let stored = test.store.fetch("job-empty-success").expect("record");
    assert_eq!(stored.record.state, JobState::Failed);
    assert!(stored.artifacts.is_empty());
    assert!(stored
        .record
        .error_message
        .as_deref()
        .expect("message")
        .contains("at least one artifact"));
}

struct FaultingPutResultStore {
    inner: InMemoryJobStore,
}

impl JobStore for FaultingPutResultStore {
    fn update_progress(
        &self,
        job_id: &str,
        snapshot: harness_contracts::progress::ExecutionProgress,
        now: chrono::DateTime<Utc>,
    ) -> Result<JobRecord, StoreError> {
        self.inner.update_progress(job_id, snapshot, now)
    }

    fn mark_running(
        &self,
        job_id: &str,
        started: chrono::DateTime<Utc>,
    ) -> Result<JobRecord, StoreError> {
        self.inner.mark_running(job_id, started)
    }

    fn get(&self, job_id: &str) -> Option<JobRecord> {
        self.inner.get(job_id)
    }

    fn admit(&self, record: JobRecord) -> Result<Admission, StoreError> {
        self.inner.admit(record)
    }

    fn put_result(
        &self,
        record: JobRecord,
        artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord, StoreError> {
        if !artifacts.is_empty() {
            return Err(StoreError::Io("artifact publication fault".to_string()));
        }
        self.inner.put_result(record, artifacts)
    }

    fn list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy_id: Option<&str>,
    ) -> Vec<JobRecord> {
        self.inner.list(kind, state, strategy_id)
    }

    fn fetch(&self, job_id: &str) -> Option<StoredJob> {
        self.inner.fetch(job_id)
    }

    fn delete(&self, job_id: &str) -> bool {
        self.inner.delete(job_id)
    }
}

#[tokio::test]
async fn memory_artifact_publication_fault_cannot_publish_success() {
    let store = Arc::new(FaultingPutResultStore {
        inner: InMemoryJobStore::new(),
    });
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let config = AppConfig {
        bearer_token: Some(BEARER.to_string()),
        job_store: store.clone(),
        launcher: Arc::new(InProcessLauncher::new(
            store.clone(),
            Arc::new(StubRunner { fail: false }),
            clock.clone(),
        )),
        screener_launcher: Arc::new(api::http::screener::InProcessScreenLauncher::new(
            store.clone(),
            Arc::new(api::http::screener::UnavailableScreeningRunner),
            clock.clone(),
        )),
        sas_issuer: None,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
        mcp_server: None,
    };
    let router = create_app(config).expect("app");

    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("job-memory-fault"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "job-memory-fault", "state": "failed"})
    );
    let stored = store.fetch("job-memory-fault").expect("record");
    assert_eq!(stored.record.state, JobState::Failed);
    assert!(stored.artifacts.is_empty());
    assert!(stored
        .record
        .error_message
        .as_deref()
        .expect("message")
        .contains("artifact publication fault"));
}

struct PartialArtifactRunner;

impl BacktestRunner for PartialArtifactRunner {
    fn run(
        &self,
        _configuration: &ValidatedConfiguration,
        _progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        Ok(RunOutcome {
            completed: true,
            artifacts: BTreeMap::from([
                ("a-ok.bin".to_string(), b"partial".to_vec()),
                ("z-blocked.bin".to_string(), b"blocked".to_vec()),
            ]),
        })
    }
}

#[tokio::test]
async fn filesystem_artifact_publication_fault_preserves_partial_evidence() {
    let root = temp_root("publication-fault");
    let blocked = root
        .join("objects")
        .join("jobs")
        .join("job-publication-fault")
        .join("z-blocked.bin");
    std::fs::create_dir_all(&blocked).expect("blocked artifact path");
    let router = durable_router(&root, Arc::new(PartialArtifactRunner));

    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("job-publication-fault"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "job-publication-fault", "state": "failed"})
    );

    let store = FileSystemJobStore::new(&root);
    let stored = store.fetch("job-publication-fault").expect("record");
    assert_eq!(stored.record.state, JobState::Failed);
    assert!(stored
        .record
        .error_message
        .as_deref()
        .expect("message")
        .contains("StoreError"));
    assert_eq!(
        stored.artifacts.get("a-ok.bin").map(Vec::as_slice),
        Some(&b"partial"[..])
    );
    assert!(!stored.artifacts.contains_key("z-blocked.bin"));

    let (list_status, _, list_body) = send(
        &router,
        get_auth("/v1/jobs/job-publication-fault/artifacts", BEARER),
    )
    .await;
    assert_eq!(list_status, StatusCode::OK);
    let artifacts = json_body(&list_body)["artifacts"].clone();
    assert_eq!(artifacts.as_array().map(Vec::len), Some(1));
    assert_eq!(artifacts[0]["name"], "a-ok.bin");

    let _ = std::fs::remove_dir_all(&root);
}

#[tokio::test]
async fn empty_artifact_listing_distinguishes_empty_from_absent() {
    let test = app_with(Some(BEARER), Arc::new(StubRunner { fail: false }));
    seed(
        test.store.as_ref(),
        "job-empty",
        JobState::Failed,
        "momentum",
        &[],
    );

    let (status, _, body) = send(
        &test.router,
        get_auth("/v1/jobs/job-empty/artifacts", BEARER),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "job-empty", "artifacts": []})
    );
    let stored = test.store.fetch("job-empty").expect("existing job");
    assert_eq!(stored.record.state, JobState::Failed);
    assert!(stored.artifacts.is_empty());
    assert!(test.store.fetch("job-absent").is_none());

    let (missing_status, _, missing) = send(
        &test.router,
        get_auth("/v1/jobs/job-absent/artifacts", BEARER),
    )
    .await;
    assert_eq!(missing_status, StatusCode::NOT_FOUND);
    assert_eq!(json_body(&missing)["error"]["code"], "job_not_found");
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
    let store = Arc::new(InMemoryJobStore::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let config = AppConfig {
        bearer_token: Some(BEARER.to_string()),
        job_store: store.clone(),
        launcher,
        screener_launcher: Arc::new(api::http::screener::InProcessScreenLauncher::new(
            store,
            Arc::new(api::http::screener::UnavailableScreeningRunner),
            clock.clone(),
        )),
        sas_issuer: None,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
        mcp_server: None,
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
    let (status, _, body) = send(&test.router, get_auth("/v1/jobs/unknown", BEARER)).await;
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
    let store = InMemoryJobStore::new();
    let mut artifacts = BTreeMap::new();
    artifacts.insert("a".to_string(), vec![1, 2]);
    store
        .admit(JobRecord::new("r", JobKind::Backtest, "input", Utc::now()))
        .expect("admit");
    let mut record = store.get("r").expect("admitted");
    record.state = JobState::Succeeded;
    let record = store.put_result(record, artifacts).expect("put");
    assert_eq!(record.total_bytes, 2);
    assert_eq!(store.list(None, None, Some("s")).len(), 0);
    assert_eq!(store.list(Some(JobKind::Backtest), None, None).len(), 1);
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

    let (run_status, _, run_body) = send(&router, get_auth("/v1/jobs/run-durable", BEARER)).await;
    assert_eq!(run_status, StatusCode::OK);
    assert_eq!(json_body(&run_body)["state"], "succeeded");
    assert_eq!(json_body(&run_body)["strategy_id"], "spy-momentum");

    let (job_status, _, job_body) = send(&router, get_auth("/v1/jobs/run-durable", BEARER)).await;
    assert_eq!(job_status, StatusCode::OK);
    assert_eq!(json_body(&job_body)["state"], "succeeded");

    let (artifact_status, _, artifact_body) = send(
        &router,
        get_auth("/v1/jobs/run-durable/artifacts/backtest.json", BEARER),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    assert_eq!(
        json_body(&artifact_body)["manifest"]["job_id"],
        "run-durable"
    );

    let _ = std::fs::remove_dir_all(&root);
}

#[tokio::test]
async fn native_engine_launch_reaches_a_terminal_state() {
    let root = temp_root("native");
    let server = spy_lake();
    let router = durable_router(
        &root,
        Arc::new(api::http::native::NativeBacktestRunner::with_lake_client(
            root.join("out"),
            lake_client(&server),
        )),
    );
    let momentum_source = include_str!("fixtures/parity_momentum.rs");
    let document = json!({
        "schema_version": 1,
        "shell": "backtest",
        "job_id": "native-api-run",
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-04T21:00:00Z",
        "seed": 7,
        "data": {"symbols": ["SPY"],
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]},
        "strategy_instances": [{
            "instance_id": "spy-momentum",
            "implementation": "jit",
            "bundle_hash": backtest::sha256::digest_hex(momentum_source.replace("\r\n", "\n").as_bytes()),
            "capital_fraction": 1.0,
            "symbols": ["SPY"],
            "parameters": {"source": momentum_source}
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

    let (run_status, _, run_body) =
        send(&router, get_auth("/v1/jobs/native-api-run", BEARER)).await;
    assert_eq!(run_status, StatusCode::OK);
    assert_eq!(json_body(&run_body)["state"], "succeeded");

    let (artifact_status, _, artifact_body) = send(
        &router,
        get_auth("/v1/jobs/native-api-run/artifacts/backtest.json", BEARER),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    assert_eq!(json_body(&artifact_body)["status"], "completed");
    assert_eq!(
        json_body(&artifact_body)["manifest"]["job_id"],
        "native-api-run"
    );

    let _ = std::fs::remove_dir_all(&root);
}

#[tokio::test]
async fn native_research_metrics_are_queryable_over_http() {
    let root = temp_root("native-research");
    let server = spy_lake();
    let router = durable_router(
        &root,
        Arc::new(api::http::native::NativeBacktestRunner::with_lake_client(
            root.join("out"),
            lake_client(&server),
        )),
    );
    let study_source = include_str!("fixtures/parity_study.rs");
    let document = json!({
        "schema_version": 1,
        "shell": "study",
        "job_id": "native-research-run",
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-04T21:00:00Z",
        "seed": 7,
        "data": {"symbols": ["SPY"],
            "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}]},
        "strategy_instances": [{
            "instance_id": "research-model",
            "implementation": "jit",
            "bundle_hash": backtest::sha256::digest_hex(study_source.replace("\r\n", "\n").as_bytes()),
            "capital_fraction": "1.0",
            "symbols": ["SPY"],
            "parameters": {"source": study_source}
        }]
    });

    let (status, _, body) = send(
        &router,
        post_json("/v1/studies/jobs", Some(BEARER), document),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(json_body(&body)["state"], "succeeded");

    let (run_status, _, run_body) =
        send(&router, get_auth("/v1/jobs/native-research-run", BEARER)).await;
    assert_eq!(run_status, StatusCode::OK);
    let observed = json_body(&run_body)["metrics"]["bar_observations"]
        .as_str()
        .and_then(|value| value.parse::<f64>().ok())
        .expect("custom metric");
    assert_eq!(observed, 3.0, "the three served bars must fold");

    let (artifact_status, _, artifact_body) = send(
        &router,
        get_auth(
            "/v1/jobs/native-research-run/artifacts/metrics.json",
            BEARER,
        ),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    assert_eq!(
        json_body(&artifact_body)["custom"]["bar_observations"]
            .as_str()
            .and_then(|value| value.parse::<f64>().ok()),
        Some(observed)
    );
    let _ = std::fs::remove_dir_all(&root);
}

const SUBSCRIPTION_STUDY: &str = r#"
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{export_study_program, BarFrame, MarketConsumer, MetricPublisher,
    ObservationContext, QuoteEvent, StudyProgram, Subscription};
struct Observer { quotes: u64, bars: u64 }
impl StudyProgram for Observer {
    fn new(_: Value) -> Self { Self { quotes: 0, bars: 0 } }
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("quote_observations", self.quotes as f64);
        let _ = metrics.publish("bar_observations", self.bars as f64);
    }
}
impl MarketConsumer for Observer {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote { symbols: vec!["SPY".into()] } /* BARS */]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) { self.quotes += 1; }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        self.bars += frame.bars_by_symbol.len() as u64;
    }
}
export_study_program!(Observer);
"#;

#[test]
fn authored_subscription_studies_validate_then_launch_exact_lake_inputs() {
    use mcp::tools::StudyValidator;

    let runtime = tokio::runtime::Runtime::new().expect("runtime");
    for mixed in [false, true] {
        let server = spy_lake();
        let root = temp_root(if mixed { "mixed-study" } else { "ticks-study" });
        let source = if mixed {
            SUBSCRIPTION_STUDY.replace(
                "/* BARS */",
                ", Subscription::Bars { symbols: vec![\"SPY\".into()], interval: \"1min\".into() }",
            )
        } else {
            SUBSCRIPTION_STUDY.to_string()
        };
        let expected = if mixed {
            json!([
                {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"},
                {"data_type": "quote", "symbols": ["SPY"]}
            ])
        } else {
            json!([{"data_type": "quote", "symbols": ["SPY"]}])
        };
        let model = json!({"source": source, "harnessConfiguration": {
            "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
            "data": {"subscriptions": expected}
        }});
        let validation = api::local::NativeStudyValidator
            .validate(&model)
            .expect("validate source union");
        assert_eq!(validation["instances"], 1);
        assert_eq!(validation["subscriptions"], expected);
        assert!(
            server.requests().is_empty(),
            "validation must not fetch data"
        );
        let mut document = api::local::authored_study_document(&model).expect("authored document");
        assert_eq!(document["data"]["subscriptions"], expected);
        assert!(document["data"].get("bar_interval").is_none());
        document["job_id"] = json!("job-authored-subscription-study");
        let job_id = document["job_id"].as_str().unwrap().to_string();
        let router = durable_router(
            &root,
            Arc::new(api::http::native::NativeBacktestRunner::with_lake_client(
                root.join("out"),
                lake_client(&server),
            )),
        );
        runtime.block_on(async {
            let mut mismatch = document.clone();
            mismatch["job_id"] = json!(format!("{job_id}-mismatch"));
            mismatch["data"]["subscriptions"] = json!([{"data_type": "trade", "symbols": ["SPY"]}]);
            let (status, _, body) = send(
                &router,
                post_json("/v1/studies/jobs", Some(BEARER), mismatch),
            )
            .await;
            assert_eq!(status, StatusCode::ACCEPTED);
            assert_eq!(json_body(&body)["state"], "failed");
            assert!(
                server.requests().is_empty(),
                "echo mismatch must fail before any lake traffic"
            );
            let (status, _, body) = send(
                &router,
                post_json("/v1/studies/jobs", Some(BEARER), document),
            )
            .await;
            assert_eq!(
                status,
                StatusCode::ACCEPTED,
                "{}",
                String::from_utf8_lossy(&body)
            );
            let (_, _, job) = send(&router, get_auth(&format!("/v1/jobs/{job_id}"), BEARER)).await;
            assert_eq!(
                json_body(&job)["state"],
                "succeeded",
                "{}",
                String::from_utf8_lossy(&job)
            );
            let (_, _, artifact) = send(
                &router,
                get_auth(&format!("/v1/jobs/{job_id}/artifacts/study.json"), BEARER),
            )
            .await;
            let artifact = json_body(&artifact);
            assert_eq!(artifact["manifest"]["effective_subscriptions"], expected);
            let (_, _, run) = send(&router, get_auth(&format!("/v1/jobs/{job_id}"), BEARER)).await;
            let metrics = json_body(&run)["metrics"].clone();
            assert!(
                metrics["quote_observations"]
                    .as_str()
                    .unwrap()
                    .parse::<f64>()
                    .unwrap()
                    > 0.0
            );
            assert_eq!(
                metrics["bar_observations"]
                    .as_str()
                    .unwrap()
                    .parse::<f64>()
                    .unwrap(),
                if mixed { 3.0 } else { 0.0 }
            );
        });
        let mut ensured = Vec::new();
        let mut read = Vec::new();
        for request in server.requests() {
            if request.path == "/v1/catalog/ensure" {
                let body = json_body(request.body.as_ref().unwrap());
                ensured.push(body["data_type"].as_str().unwrap().to_string());
                assert_eq!(body["start_date"], "2024-01-02");
                assert_eq!(body["end_date"], "2024-01-04");
            }
            if request.path == "/v1/read" {
                read.push(request.query["data_type"].clone());
            }
        }
        ensured.sort();
        read.sort();
        let types: Vec<String> = if mixed {
            vec!["bars".into(), "quote".into()]
        } else {
            vec!["quote".into()]
        };
        assert_eq!(ensured, types);
        assert_eq!(read, types);
        let _ = std::fs::remove_dir_all(&root);
    }
}

#[test]
fn authored_tick_echo_uses_only_explicit_symbols_and_rejects_mismatch() {
    use mcp::tools::StudyValidator;
    let model = json!({
        "source": SUBSCRIPTION_STUDY.replace("SPY", "QQQ"),
        "harnessConfiguration": {
            "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
            "subscriptions": [{"data_type": "quote", "symbols": ["QQQ"]}]
        }
    });
    let document = api::local::authored_study_document(&model).unwrap();
    assert_eq!(document["data"]["symbols"], json!(["QQQ"]));
    assert_eq!(document["strategy_instances"][0]["symbols"], json!(["QQQ"]));
    let response = api::local::NativeStudyValidator.validate(&model).unwrap();
    assert_eq!(
        response["subscriptions"],
        model["harnessConfiguration"]["subscriptions"]
    );
    let mut mismatch = model;
    mismatch["harnessConfiguration"]["subscriptions"][0]["data_type"] = json!("trade");
    assert!(api::local::NativeStudyValidator
        .validate(&mismatch)
        .unwrap_err()
        .contains("does not match"));
}

#[test]
fn validator_returns_recursive_indicator_requirements_without_extras() {
    use mcp::tools::StudyValidator;
    let source = r#"
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{export_study_program, Decimal, Indicator, IndicatorSequence, MarketConsumer,
    ObservationContext, StudyProgram, Subscription};
struct Leaf { last: Option<Decimal>, sequence: IndicatorSequence }
impl Indicator for Leaf { fn value(&self, _: &str) -> Option<Decimal> { self.last }
fn sequence(&self) -> &IndicatorSequence { &self.sequence }
fn sequence_mut(&mut self) -> &mut IndicatorSequence { &mut self.sequence } }
impl MarketConsumer for Leaf {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Trade { symbols: vec!["QQQ".into()] }]
    }
}
struct Parent { leaf: Leaf, sequence: IndicatorSequence }
impl Indicator for Parent { fn value(&self, symbol: &str) -> Option<Decimal> { self.leaf.value(symbol) }
fn sequence(&self) -> &IndicatorSequence { &self.sequence }
fn sequence_mut(&mut self) -> &mut IndicatorSequence { &mut self.sequence } }
impl MarketConsumer for Parent {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> { vec![] }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> { vec![&mut self.leaf] }
}
struct Study { parent: Parent }
impl StudyProgram for Study {
    fn new(_: Value) -> Self { Self { parent: Parent { leaf: Leaf { last: None, sequence: IndicatorSequence::ticks(60_000_000_000).unwrap() }, sequence: IndicatorSequence::ticks(60_000_000_000).unwrap() } } }
}
impl MarketConsumer for Study {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> { vec![] }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> { vec![&mut self.parent] }
}
export_study_program!(Study);
"#;
    let expected = json!([{"data_type": "trade", "symbols": ["QQQ"]}]);
    let model = json!({"source": source, "harnessConfiguration": {
        "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
        "subscriptions": expected
    }});
    let response = api::local::NativeStudyValidator.validate(&model).unwrap();
    assert_eq!(response["subscriptions"], expected);
}

#[test]
fn strict_http_subscription_contract_rejects_invalid_shapes() {
    use api::http::configuration::ConfigurationParser;
    for subscriptions in [
        json!({"data_type": "quote", "symbols": ["SPY"]}),
        json!([{"data_type": "bars", "symbols": ["SPY"]}]),
        json!([{"data_type": "bars", "symbols": ["SPY"], "interval": "1s"}]),
        json!([{"data_type": "quote", "symbols": ["SPY"], "interval": null}]),
        json!([{"data_type": "trade", "symbols": []}]),
        json!([{"data_type": "quote", "symbols": ["SPY"], "extra": true}]),
    ] {
        let mut document = backtest_document("invalid-subscription");
        document["data"] = json!({"subscriptions": subscriptions});
        assert!(StrictJsonConfigurationParser
            .parse(&serde_json::to_vec(&document).unwrap())
            .is_err());
    }
}

#[test]
fn authored_contract_rejects_symbol_and_interval_conflicts_before_lake() {
    use mcp::tools::StudyValidator;

    let server = spy_lake();
    let base = json!({"source": SUBSCRIPTION_STUDY, "harnessConfiguration": {
        "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
        "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]
    }});
    for symbols in [json!(["QQQ"]), json!([]), json!("SPY"), json!([1])] {
        let mut model = base.clone();
        model["harnessConfiguration"]["symbols"] = symbols;
        assert!(api::local::authored_study_document(&model)
            .unwrap_err()
            .contains("symbols"));
        assert!(api::local::NativeStudyValidator.validate(&model).is_err());
    }
    let mut ticks = base.clone();
    ticks["harnessConfiguration"]["interval"] = json!("1min");
    assert!(api::local::NativeStudyValidator
        .validate(&ticks)
        .unwrap_err()
        .contains("interval"));

    let mut bars = base;
    bars["source"] = json!(include_str!("fixtures/parity_study.rs"));
    bars["harnessConfiguration"]["subscriptions"] = json!([
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1d"}
    ]);
    assert!(api::local::NativeStudyValidator
        .validate(&bars)
        .unwrap_err()
        .contains("does not match"));
    bars["harnessConfiguration"]["subscriptions"] = json!([
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
    ]);
    bars["harnessConfiguration"]["symbols"] = json!(["SPY", "SPY"]);
    assert!(api::local::NativeStudyValidator.validate(&bars).is_ok());
    assert!(server.requests().is_empty());
}

#[test]
fn authored_subscriptions_must_match_compiled_bar_requirements() {
    use mcp::tools::StudyValidator;

    let server = spy_lake();
    let model = json!({"source": include_str!("fixtures/parity_study.rs"), "harnessConfiguration": {
        "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
        "symbols": ["SPY"],
        "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}]
    }});
    let response = api::local::NativeStudyValidator.validate(&model).unwrap();
    assert_eq!(
        response["subscriptions"],
        json!([
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
        ])
    );
    let mut wrong_symbols = model.clone();
    wrong_symbols["harnessConfiguration"]["subscriptions"][0]["symbols"] = json!(["QQQ"]);
    let mut wrong_interval = model.clone();
    wrong_interval["harnessConfiguration"]["subscriptions"][0]["interval"] = json!("1d");
    let mut wrong_type = model;
    wrong_type["source"] = json!(SUBSCRIPTION_STUDY);
    for mismatch in [wrong_symbols, wrong_interval, wrong_type] {
        assert!(api::local::NativeStudyValidator
            .validate(&mismatch)
            .unwrap_err()
            .contains("match"));
    }
    assert!(server.requests().is_empty());
}

#[test]
fn nested_authored_configuration_preserves_runtime_fields_and_normalized_source() {
    use mcp::tools::StudyValidator;

    let source = include_str!("fixtures/parity_study.rs")
        .replace("\r\n", "\n")
        .replace("SPY", "QQQ");
    let model = json!({"source": source.replace('\n', "\r\n"), "harnessConfiguration": {
        "start": "2024-01-02T09:30:00-05:00", "end": "2024-01-04T16:00:00-05:00",
        "seed": 31, "base_currency": "EUR", "initial_cash": "12345.50",
        "data": {"symbols": ["QQQ"],
            "subscriptions": [{"data_type": "bars", "symbols": ["QQQ"], "interval": "1min"}],
            "snapshot_id": "authored-snapshot", "fingerprint": "b".repeat(64)},
        "strategy_instances": [{"parameters": {"source": "stale"}}],
        "shell": "backtest", "job_id": "stale"
    }});
    let document = api::local::authored_study_document(&model).unwrap();
    for key in [
        "start",
        "end",
        "seed",
        "base_currency",
        "initial_cash",
        "data",
    ] {
        assert_eq!(document[key], model["harnessConfiguration"][key], "{key}");
    }
    assert_eq!(document["data"]["symbols"], json!(["QQQ"]));
    assert_eq!(document["shell"], "study");
    assert!(
        document.get("job_id").is_none(),
        "authored documents never carry a stale execution identity"
    );
    assert_eq!(
        document["strategy_instances"][0]["parameters"],
        json!({"source": source})
    );
    assert_eq!(
        document["strategy_instances"][0]["bundle_hash"],
        backtest::sha256::digest_hex(source.as_bytes())
    );
    let response = api::local::NativeStudyValidator.validate(&model).unwrap();
    assert_eq!(
        response["subscriptions"],
        json!([
            {"data_type": "bars", "symbols": ["QQQ"], "interval": "1min"}
        ])
    );
    let mut lf_model = model.clone();
    lf_model["source"] = json!(source);
    assert_eq!(
        api::local::authored_study_document(&lf_model).unwrap(),
        document
    );
    for alias in ["symbols", "requiredSymbols", "required_symbols"] {
        let mut aliased = model.clone();
        aliased["harnessConfiguration"][alias] = json!(["QQQ", "QQQ"]);
        assert_eq!(
            api::local::authored_study_document(&aliased).unwrap(),
            document
        );
        aliased["harnessConfiguration"][alias] = json!(["SPY"]);
        assert!(api::local::NativeStudyValidator
            .validate(&aliased)
            .unwrap_err()
            .contains("must match"));
    }
}

#[test]
fn nested_and_shorthand_contracts_reject_contradictions_before_lake() {
    use mcp::tools::StudyValidator;

    let server = spy_lake();
    let echo = json!([
        {"data_type": "quote", "symbols": ["SPY"]},
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
    ]);
    let source = SUBSCRIPTION_STUDY.replace(
        "/* BARS */",
        ", Subscription::Bars { symbols: vec![\"SPY\".into()], interval: \"1min\".into() }",
    );
    let model = json!({"source": source, "harnessConfiguration": {
        "start": "2024-01-02T14:30:00Z", "end": "2024-01-04T21:00:00Z",
        "data": {"subscriptions": echo, "symbols": ["SPY"]}
    }});
    let document = api::local::authored_study_document(&model).unwrap();
    assert_eq!(document["data"], model["harnessConfiguration"]["data"]);
    assert!(api::local::NativeStudyValidator.validate(&model).is_ok());
    let mut equivalent = model.clone();
    equivalent["harnessConfiguration"]["subscriptions"] = json!([
        {"data_type": "bars", "symbols": ["SPY", "SPY"], "interval": "1min"},
        {"data_type": "quote", "symbols": ["SPY"]}
    ]);
    let document = api::local::authored_study_document(&equivalent).unwrap();
    assert!(document.get("subscriptions").is_none());
    assert!(document.get("interval").is_none());
    assert_eq!(
        document["data"]["subscriptions"],
        equivalent["harnessConfiguration"]["subscriptions"]
    );
    let mut wrong_echo = model.clone();
    wrong_echo["harnessConfiguration"]["subscriptions"] =
        json!([{"data_type": "trade", "symbols": ["SPY"]}]);
    let mut wrong_interval = model.clone();
    wrong_interval["harnessConfiguration"]["interval"] = json!("1d");
    let mut wrong_symbols = model.clone();
    wrong_symbols["harnessConfiguration"]["requiredSymbols"] = json!(["QQQ"]);
    let mut wrong_union = model;
    wrong_union["harnessConfiguration"]["data"]["symbols"] = json!(["QQQ"]);
    for mismatch in [wrong_echo, wrong_interval, wrong_symbols, wrong_union] {
        assert!(api::local::NativeStudyValidator
            .validate(&mismatch)
            .is_err());
    }
    assert!(server.requests().is_empty());
}

struct IncompleteRunner;

impl BacktestRunner for IncompleteRunner {
    fn run(
        &self,
        _configuration: &ValidatedConfiguration,
        _progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "backtest.json".to_string(),
            b"{\"status\":\"failed\"}".to_vec(),
        );
        Ok(RunOutcome {
            completed: false,
            artifacts,
        })
    }
}

struct CountingRunner {
    runs: Arc<AtomicUsize>,
}

impl BacktestRunner for CountingRunner {
    fn run(
        &self,
        _configuration: &ValidatedConfiguration,
        _progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        self.runs.fetch_add(1, Ordering::SeqCst);
        let mut artifacts = BTreeMap::new();
        artifacts.insert("backtest.json".to_string(), b"{}".to_vec());
        Ok(RunOutcome {
            completed: true,
            artifacts,
        })
    }
}

#[tokio::test]
async fn not_completed_outcome_is_recorded_as_failed() {
    let test = app_with(Some(BEARER), Arc::new(IncompleteRunner));
    let (status, _, body) = send(
        &test.router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("job-incomplete"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(
        json_body(&body),
        json!({"job_id": "job-incomplete", "state": "failed"})
    );
    assert_eq!(
        test.store.get("job-incomplete").expect("record").state,
        JobState::Failed
    );
    let stored = test.store.fetch("job-incomplete").expect("stored job");
    assert_eq!(stored.record.state, JobState::Failed);
    assert_eq!(stored.record.artifact_count, 1);
    assert!(stored.artifacts.contains_key("backtest.json"));
}

#[tokio::test]
async fn duplicate_submission_joins_and_conflicting_input_fails_closed() {
    let runs = Arc::new(AtomicUsize::new(0));
    let test = app_with(
        Some(BEARER),
        Arc::new(CountingRunner { runs: runs.clone() }),
    );
    let document = backtest_document("job-duplicate");
    let (_, _, first) = send(
        &test.router,
        post_json("/v1/backtests/jobs", Some(BEARER), document.clone()),
    )
    .await;
    assert_eq!(json_body(&first)["state"], "succeeded");
    let (_, _, second) = send(
        &test.router,
        post_json("/v1/backtests/jobs", Some(BEARER), document),
    )
    .await;
    assert_eq!(json_body(&second)["state"], "succeeded");
    assert_eq!(
        runs.load(Ordering::SeqCst),
        1,
        "a duplicate submission must not dispatch again"
    );

    let mut conflicting = backtest_document("job-duplicate");
    conflicting["seed"] = json!(99);
    let (status, _, body) = send(
        &test.router,
        post_json("/v1/backtests/jobs", Some(BEARER), conflicting),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(json_body(&body)["error"]["code"], "job_conflict");
    assert_eq!(runs.load(Ordering::SeqCst), 1);
}

#[test]
fn admission_and_result_publication_rules_are_atomic() {
    let store = InMemoryJobStore::new();
    let now = Utc::now();
    let created = store
        .admit(JobRecord::new("job-rules", JobKind::Study, "digest", now))
        .expect("create");
    assert!(created.created());
    let joined = store
        .admit(JobRecord::new("job-rules", JobKind::Study, "digest", now))
        .expect("join");
    assert!(!joined.created());
    assert!(matches!(
        store.admit(JobRecord::new("job-rules", JobKind::Study, "other", now)),
        Err(StoreError::Conflict(_))
    ));

    let mut record = store.get("job-rules").expect("record");
    record.state = JobState::Succeeded;
    assert!(matches!(
        store.put_result(record.clone(), BTreeMap::new()),
        Err(StoreError::EmptyArtifacts)
    ));
    let artifacts = BTreeMap::from([("study.json".to_string(), b"{}".to_vec())]);
    store
        .put_result(record.clone(), artifacts.clone())
        .expect("publish");
    let mut again = store.get("job-rules").expect("record");
    again.state = JobState::Succeeded;
    assert!(matches!(
        store.put_result(again, artifacts),
        Err(StoreError::Terminal(_))
    ));
}

struct FailingAdmitStore;

impl JobStore for FailingAdmitStore {
    fn update_progress(
        &self,
        job_id: &str,
        snapshot: harness_contracts::progress::ExecutionProgress,
        now: chrono::DateTime<Utc>,
    ) -> Result<JobRecord, StoreError> {
        let _ = (job_id, snapshot, now);
        Err(StoreError::Io("disk offline".to_string()))
    }

    fn mark_running(
        &self,
        _job_id: &str,
        _started: chrono::DateTime<Utc>,
    ) -> Result<JobRecord, StoreError> {
        Err(StoreError::Io("disk offline".to_string()))
    }

    fn get(&self, _job_id: &str) -> Option<JobRecord> {
        None
    }

    fn admit(&self, _record: JobRecord) -> Result<Admission, StoreError> {
        Err(StoreError::Io("disk offline".to_string()))
    }

    fn put_result(
        &self,
        _record: JobRecord,
        _artifacts: BTreeMap<String, Vec<u8>>,
    ) -> Result<JobRecord, StoreError> {
        Err(StoreError::Io("disk offline".to_string()))
    }

    fn list(
        &self,
        _kind: Option<JobKind>,
        _state: Option<JobState>,
        _strategy_id: Option<&str>,
    ) -> Vec<JobRecord> {
        Vec::new()
    }

    fn fetch(&self, _job_id: &str) -> Option<StoredJob> {
        None
    }

    fn delete(&self, _job_id: &str) -> bool {
        false
    }
}

#[tokio::test]
async fn admission_store_failure_is_not_reported_as_accepted() {
    let mut config = AppConfig::in_memory();
    config.bearer_token = Some(BEARER.to_string());
    config.job_store = Arc::new(FailingAdmitStore);
    let router = create_app(config).expect("app");
    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            backtest_document("job-store-failure"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(json_body(&body)["error"]["code"], "store_error");
}

/// The execution port reports the same telemetry for direct HTTP launches and workflow callers.
struct ProgressThenFailure;
impl BacktestRunner for ProgressThenFailure {
    fn run(
        &self,
        _configuration: &ValidatedConfiguration,
        progress: harness_contracts::progress::ProgressReporter,
    ) -> Result<RunOutcome, String> {
        use harness_contracts::progress::{ExecutionProgress, ExecutionStage};
        let mut acquisition = ExecutionProgress::stage(ExecutionStage::PreparingData);
        acquisition.symbol = Some("MSFT".into());
        acquisition.data_type = Some("quote".into());
        acquisition.sessions_total = Some(137);
        acquisition.sessions_ready = Some(40);
        acquisition.sessions_cached = Some(40);
        progress(acquisition.clone())?;
        acquisition.sessions_ready = Some(137);
        acquisition.rows = 54_782_541;
        acquisition.completed = true;
        progress(acquisition)?;
        let mut reading = ExecutionProgress::stage(ExecutionStage::ReadingData);
        reading.symbol = Some("MSFT".into());
        reading.data_type = Some("quote".into());
        reading.bytes = 123456;
        reading.request_timeout_seconds = Some(60);
        progress(reading)?;
        Err("reading MSFT quote after 123456 bytes: idle read timed out after 60 seconds".into())
    }
}

#[tokio::test]
async fn direct_launch_status_retains_progress_and_error_without_a_workflow() {
    use harness_contracts::progress::{ExecutionProgress, ExecutionStage};
    let app = app_with(Some(BEARER), Arc::new(ProgressThenFailure));
    let original = backtest_document("job-progress");
    let _ = app
        .router
        .clone()
        .oneshot(post_json(
            "/v1/backtests/jobs",
            Some(BEARER),
            original.clone(),
        ))
        .await
        .unwrap();
    let (status, _, bytes) = send(&app.router, get_auth("/v1/jobs/job-progress", BEARER)).await;
    assert_eq!(status, StatusCode::OK);
    let body = json_body(&bytes);
    assert_eq!(body["job_id"], "job-progress");
    assert_eq!(body["state"], "failed");
    assert_eq!(body["progress"][0]["sessions_cached"], 40);
    assert_eq!(body["progress"][0]["sessions_ready"], 137);
    assert_eq!(body["progress"][0]["rows"], 54_782_541);
    assert_eq!(body["progress"][1]["bytes"], 123456);
    assert!(body["progress"][1]["finished_at"].is_string());
    assert!(body["error_message"]
        .as_str()
        .unwrap()
        .contains("idle read timed out"));
    let before = app.store.get("job-progress").unwrap();
    assert!(matches!(
        app.store.update_progress(
            "job-progress",
            ExecutionProgress::stage(ExecutionStage::RunningBacktest),
            Utc::now()
        ),
        Err(StoreError::Terminal(_))
    ));
    assert_eq!(app.store.get("job-progress").unwrap(), before);
    // Joining a terminal execution does not dispatch it or replace its evidence.
    let _ = app
        .router
        .clone()
        .oneshot(post_json("/v1/backtests/jobs", Some(BEARER), original))
        .await
        .unwrap();
    assert_eq!(app.store.get("job-progress").unwrap(), before);
}
