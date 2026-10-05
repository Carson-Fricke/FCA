//! Remote MCP transport tests for the axum HTTP boundary.
//!
//! These drive the in-process `tower` oneshot harness, so the whole surface is
//! exercised without a socket. The MCP routes reuse the JSON-RPC server and the
//! shared durable store, so a remote client observes the same runs and jobs as
//! the REST routes.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use api::http::configuration::ValidatedConfiguration;
use api::http::create_app;
use api::http::launcher::{BacktestRunner, RunOutcome};
use api::http::state::AppConfig;
use api::http::store::{
    FileSystemJobRepository, FileSystemRunStore, JobRecord, JobRepository, JobState,
    RunCatalogRecord, RunStatus, RunStore,
};
use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tower::ServiceExt;

const BEARER: &str = "test-secret";

struct StubRunner;

impl BacktestRunner for StubRunner {
    fn run(&self, _configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        let mut artifacts = BTreeMap::new();
        artifacts.insert(
            "backtest.json".to_string(),
            b"{\"status\":\"completed\"}".to_vec(),
        );
        Ok(RunOutcome {
            completed: true,
            artifacts,
        })
    }
}

fn temp_root(tag: &str) -> PathBuf {
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let root = std::env::temp_dir().join(format!(
        "harness-mcp-http-{}-{tag}-{unique}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}

fn durable_app(root: &Path) -> Router {
    let mut config = AppConfig::durable_with_runner(root.to_path_buf(), Arc::new(StubRunner));
    config.bearer_token = Some(BEARER.to_string());
    create_app(config).expect("app")
}

fn seed_run(store: &dyn RunStore, run_id: &str) {
    let record = RunCatalogRecord::new(run_id, RunStatus::Complete, "momentum");
    let mut artifacts = BTreeMap::new();
    artifacts.insert("backtest.json".to_string(), br#"{"ok":true}"#.to_vec());
    store.put(record, artifacts).expect("seed run");
}

fn seed_job(jobs: &dyn JobRepository, job_id: &str) {
    let now = chrono::Utc::now();
    jobs.upsert(JobRecord::new(job_id, JobState::Succeeded, now, now));
}

fn get_with(path: &str, token: Option<&str>) -> Request<Body> {
    let mut builder = Request::builder().method("GET").uri(path);
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    builder.body(Body::empty()).expect("request")
}

fn post_mcp(token: Option<&str>, payload: &[u8]) -> Request<Body> {
    let mut builder = Request::builder()
        .method("POST")
        .uri("/mcp")
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    builder.body(Body::from(payload.to_vec())).expect("request")
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

fn tool_call(name: &str, arguments: Value) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }))
    .expect("request")
}

#[tokio::test]
async fn mcp_health_publishes_the_tool_catalog() {
    let root = temp_root("health");
    let router = durable_app(&root);
    let (status, body) = send(&router, get_with("/mcp/health", Some(BEARER))).await;
    assert_eq!(status, StatusCode::OK);
    let body = json_body(&body);
    assert_eq!(body["status"], "ok");
    let tools = body["tools"].as_array().expect("tools");
    assert!(tools.iter().any(|tool| tool == "get_job_status"));
    assert!(tools.iter().any(|tool| tool == "list_artifacts"));
}

#[tokio::test]
async fn mcp_tool_call_list_artifacts_reads_the_durable_store() {
    let root = temp_root("artifacts");
    let router = durable_app(&root);
    let store = FileSystemRunStore::new(&root);
    seed_run(&store, "run-mcp");

    let (status, body) = send(
        &router,
        post_mcp(
            Some(BEARER),
            &tool_call("list_artifacts", json!({"run_id": "run-mcp"})),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let body = json_body(&body);
    assert_eq!(body["result"]["isError"], false);
    assert_eq!(body["result"]["structuredContent"]["run_id"], "run-mcp");
    let artifacts = body["result"]["structuredContent"]["artifacts"]
        .as_array()
        .expect("artifacts");
    assert_eq!(artifacts.len(), 1);
    assert_eq!(artifacts[0]["name"], "backtest.json");
}

#[tokio::test]
async fn mcp_tool_call_get_job_status_reads_the_durable_jobs() {
    let root = temp_root("status");
    let router = durable_app(&root);
    let jobs = FileSystemJobRepository::new(&root);
    seed_job(&jobs, "job-mcp");

    let (status, body) = send(
        &router,
        post_mcp(
            Some(BEARER),
            &tool_call("get_job_status", json!({"job_id": "job-mcp"})),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let body = json_body(&body);
    assert_eq!(body["result"]["isError"], false);
    assert_eq!(body["result"]["structuredContent"]["job_id"], "job-mcp");
    assert_eq!(body["result"]["structuredContent"]["state"], "succeeded");
}

#[tokio::test]
async fn mcp_run_backtest_uses_the_composed_native_submitter() {
    let root = temp_root("backtest-submitter");
    let router = durable_app(&root);

    let (status, body) = send(
        &router,
        post_mcp(
            Some(BEARER),
            &tool_call("run_backtest", json!({"config_path": "missing.json"})),
        ),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    let body = json_body(&body);
    assert_eq!(body["result"]["isError"], true);
    let text = body["result"]["content"][0]["text"]
        .as_str()
        .expect("tool error text");
    assert!(text.contains("local backtest runner could not complete"));
    assert!(!text.contains("no process runner is composed"));
}

#[tokio::test]
async fn mcp_rejects_a_malformed_request_body() {
    let root = temp_root("malformed");
    let router = durable_app(&root);
    let (status, body) = send(&router, post_mcp(Some(BEARER), b"{not json")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(json_body(&body)["error"]["code"], -32700);
}

#[tokio::test]
async fn mcp_requires_a_bearer_credential() {
    let root = temp_root("auth");
    let router = durable_app(&root);
    let (post_status, _) = send(
        &router,
        post_mcp(None, &tool_call("list_artifacts", json!({"run_id": "r"}))),
    )
    .await;
    assert_eq!(post_status, StatusCode::UNAUTHORIZED);

    let (health_status, _) = send(&router, get_with("/mcp/health", None)).await;
    assert_eq!(health_status, StatusCode::UNAUTHORIZED);
}
