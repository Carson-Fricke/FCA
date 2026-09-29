//! Screening job lifecycle over the axum HTTP boundary.
//!
//! The routes are driven through the `tower` oneshot harness with in-memory
//! stores and an injected fake [`ScreeningRunner`], so the whole launch ->
//! background worker -> job/run/artifact path is exercised without a socket,
//! a JIT build, or lake data.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use api::http::clock::{Clock, SystemClock};
use api::http::configuration::StrictJsonConfigurationParser;
use api::http::create_app;
use api::http::launcher::{BacktestRunner, InProcessLauncher, UnportedBacktestRunner};
use api::http::screener::{InProcessScreenLauncher, ScreenRunOutcome, ScreeningRunner};
use api::http::state::AppConfig;
use api::http::store::{InMemoryJobRepository, InMemoryRunStore};
use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;

const BEARER: &str = "test-secret";

struct FakeScreenRunner {
    fail: bool,
}

impl ScreeningRunner for FakeScreenRunner {
    fn run(
        &self,
        configuration: &backtest::backtest::screen::ScreenConfig,
        _output_dir: Option<&std::path::Path>,
    ) -> Result<ScreenRunOutcome, String> {
        let artifact = if self.fail {
            json!({
                "schema": "fca-harness-screen-v1",
                "status": "failed",
                "run_id": configuration.run_id(),
                "error": "screening data source failed: no bar coverage",
                "rows": [],
            })
        } else {
            json!({
                "schema": "fca-harness-screen-v1",
                "status": "complete",
                "run_id": configuration.run_id(),
                "rows": [
                    {"symbol": "AAPL", "metrics": {"name": "1.23"}},
                    {"symbol": "MSFT", "metrics": {"name": "0.75"}}
                ],
            })
        };
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "screen.json".to_string(),
            serde_json::to_vec(&artifact).expect("artifact"),
        );
        Ok(ScreenRunOutcome {
            completed: !self.fail,
            rows: if self.fail { 0 } else { 2 },
            artifacts,
            error: self
                .fail
                .then(|| "screening data source failed: no bar coverage".to_string()),
        })
    }
}

fn screen_document() -> Value {
    let source = "use harness_strategy_sdk::{export_screening_program, ScreenContext, ScreenRow, ScreeningProgram};\nstruct Empty;\nimpl ScreeningProgram for Empty { fn new(_p: harness_strategy_sdk::serde_json::Value) -> Self { Self } fn screen(&mut self, _c: &ScreenContext) -> Vec<ScreenRow> { Vec::new() } }\nexport_screening_program!(Empty);\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "http-screen",
        "as_of": "2024-01-19",
        "bar_interval": "1d",
        "lookback_days": 20,
        "rank_metric": "name",
        "universe_revision": "sp500-2024-01",
        "source": source,
        "source_sha256": digest,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "a".repeat(64)),
        "definition_hash": "b".repeat(64),
        "parameters": {},
    })
}

fn app(fail: bool) -> Router {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let launcher = Arc::new(InProcessLauncher::new(
        store.clone(),
        jobs.clone(),
        Arc::new(UnportedBacktestRunner) as Arc<dyn BacktestRunner>,
        clock.clone(),
    ));
    let screener_launcher = Arc::new(InProcessScreenLauncher::new(
        store.clone(),
        jobs.clone(),
        Arc::new(FakeScreenRunner { fail }),
        clock.clone(),
    ));
    create_app(AppConfig {
        bearer_token: Some(BEARER.to_string()),
        run_store: store,
        job_repository: jobs,
        launcher,
        screener_launcher,
        sas_issuer: None,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
        mcp_server: None,
    })
    .expect("app")
}

fn post_json(path: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {BEARER}"))
        .body(Body::from(serde_json::to_vec(&body).expect("json")))
        .expect("request")
}

fn get_auth(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {BEARER}"))
        .body(Body::empty())
        .expect("request")
}

async fn send(router: &Router, request: Request<Body>) -> (StatusCode, Vec<u8>) {
    let response = router.clone().oneshot(request).await.expect("oneshot");
    let status = response.status();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes()
        .to_vec();
    (status, body)
}

fn json_body(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).expect("json body")
}

async fn wait_terminal(router: &Router, job_id: &str) -> Value {
    for _ in 0..400 {
        let (status, body) = send(router, get_auth(&format!("/v1/screeners/jobs/{job_id}"))).await;
        assert_eq!(status, StatusCode::OK);
        let document = json_body(&body);
        if matches!(document["state"].as_str(), Some("succeeded" | "failed")) {
            return document;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("screener job '{job_id}' did not reach a terminal state");
}

#[tokio::test]
async fn screening_launch_runs_in_the_background_and_is_queryable() {
    let router = app(false);
    let document = screen_document();

    let (status, launch_body) = send(&router, post_json("/v1/screeners/jobs", document)).await;
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "{}",
        String::from_utf8_lossy(&launch_body)
    );
    let launch = json_body(&launch_body);
    let job_id = launch["job_id"].as_str().expect("job id").to_string();
    assert!(job_id.starts_with("screen-"), "job id: {job_id}");
    // The handler returned before the worker finished; the state is the
    // still-in-flight vocabulary at acceptance time.
    assert!(
        matches!(
            launch["state"].as_str(),
            Some("queued" | "running" | "succeeded")
        ),
        "unexpected launch state: {}",
        launch["state"]
    );

    let terminal = wait_terminal(&router, &job_id).await;
    assert_eq!(terminal["state"], "succeeded");
    assert_eq!(terminal["error_message"], Value::Null);

    let (run_status, run_body) =
        send(&router, get_auth(&format!("/v1/backtests/runs/{job_id}"))).await;
    assert_eq!(run_status, StatusCode::OK);
    let run = json_body(&run_body);
    assert_eq!(run["status"], "complete");
    assert_eq!(run["strategy_id"], format!("sha256:{}", "a".repeat(64)));
    assert_eq!(run["data_start"], "2023-12-30");
    assert_eq!(run["data_end"], "2024-01-19");
    assert_eq!(run["metrics"]["row_count"], "2");
    assert_eq!(run["metrics"]["rank_metric"], "name");
    assert!(run["configuration_digest"].is_string());

    let (artifact_status, artifact_body) = send(
        &router,
        get_auth(&format!(
            "/v1/backtests/runs/{job_id}/artifacts/screen.json"
        )),
    )
    .await;
    assert_eq!(artifact_status, StatusCode::OK);
    let artifact = json_body(&artifact_body);
    assert_eq!(artifact["schema"], "fca-harness-screen-v1");
    assert_eq!(artifact["status"], "complete");
    assert_eq!(artifact["rows"][0]["symbol"], "AAPL");
}

#[tokio::test]
async fn a_failed_screening_run_records_a_failed_job_and_run() {
    let router = app(true);
    let document = screen_document();

    let (status, launch_body) = send(&router, post_json("/v1/screeners/jobs", document)).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    let job_id = json_body(&launch_body)["job_id"]
        .as_str()
        .expect("job id")
        .to_string();

    let terminal = wait_terminal(&router, &job_id).await;
    assert_eq!(terminal["state"], "failed");
    assert!(terminal["error_message"]
        .as_str()
        .expect("error")
        .contains("no bar coverage"));

    let (run_status, run_body) =
        send(&router, get_auth(&format!("/v1/backtests/runs/{job_id}"))).await;
    assert_eq!(run_status, StatusCode::OK);
    let run = json_body(&run_body);
    assert_eq!(run["status"], "failed");
    assert_eq!(run["metrics"]["row_count"], "0");
}

#[tokio::test]
async fn screening_launch_rejects_wrong_shells_and_unknown_fields() {
    let router = app(false);

    let mut wrong_shell = screen_document();
    wrong_shell["shell"] = json!("backtest");
    let (status, body) = send(&router, post_json("/v1/screeners/jobs", wrong_shell)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&body)["error"]["code"], "validation_error");

    let mut unknown = screen_document();
    unknown["run_id"] = json!("not-allowed");
    let (status, body) = send(&router, post_json("/v1/screeners/jobs", unknown)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&body)["error"]["code"], "validation_error");

    let mut mismatch = screen_document();
    mismatch["bundle_hash"] = json!("c".repeat(64));
    let (status, body) = send(&router, post_json("/v1/screeners/jobs", mismatch)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json_body(&body)["error"]["code"], "validation_error");

    let (status, _) = send(&router, get_auth("/v1/screeners/jobs/unknown")).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}
