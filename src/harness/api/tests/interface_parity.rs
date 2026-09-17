//! Cross-interface parity: the axum HTTP boundary and the MCP server share one
//! Rust runtime.
//!
//! The test launches a backtest through the HTTP `POST /v1/backtests/jobs`
//! route and then reads the same run, job, and artifacts through MCP
//! `tools/call`, with an MCP remote transport that dispatches straight into
//! the axum router. No network or subprocess is involved.

use std::collections::BTreeMap;
use std::sync::Arc;

use api::http::clock::{Clock, SystemClock};
use api::http::configuration::{StrictJsonConfigurationParser, ValidatedConfiguration};
use api::http::create_app;
use api::http::launcher::{BacktestRunner, InProcessLauncher, RunOutcome};
use api::http::state::AppConfig;
use api::http::store::{InMemoryJobRepository, InMemoryRunStore};
use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use mcp::auth::{BearerTokenSource, MissingBearerCredentialError};
use mcp::create_server;
use mcp::server::McpServer;
use mcp::tools::{
    Boundary, ConfigurationLoader, HarnessToolsContext, RemoteResponse, RemoteTransport,
};
use serde_json::{json, Value};
use tower::ServiceExt;

const BASE_URL: &str = "https://api.local";
const BEARER: &str = "test-secret";

struct StubRunner;

impl BacktestRunner for StubRunner {
    fn run(&self, configuration: &ValidatedConfiguration) -> Result<RunOutcome, String> {
        let body = format!(
            "{{\"manifest\":{{\"run_id\":\"{}\"}},\"status\":\"completed\"}}",
            configuration.run_id
        );
        let mut artifacts = BTreeMap::new();
        artifacts.insert("backtest.json".to_string(), body.into_bytes());
        Ok(RunOutcome {
            completed: true,
            artifacts,
        })
    }
}

struct StaticCredentials;

impl BearerTokenSource for StaticCredentials {
    fn bearer_token(&self) -> Option<String> {
        Some(BEARER.to_string())
    }

    fn bearer_headers(&self) -> Result<BTreeMap<String, String>, MissingBearerCredentialError> {
        let mut headers = BTreeMap::new();
        headers.insert("Authorization".to_string(), format!("Bearer {BEARER}"));
        Ok(headers)
    }
}

fn http_app() -> Router {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let launcher = Arc::new(InProcessLauncher::new(
        store.clone(),
        jobs.clone(),
        Arc::new(StubRunner),
        clock.clone(),
    ));
    create_app(AppConfig {
        bearer_token: Some(BEARER.to_string()),
        run_store: store,
        job_repository: jobs,
        launcher,
        sas_issuer: None,
        clock,
        configuration_parser: Arc::new(StrictJsonConfigurationParser),
        mcp_server: None,
    })
    .expect("app")
}

/// An MCP remote transport that dispatches directly into an axum router.
struct ApiTransport {
    router: Router,
}

impl RemoteTransport for ApiTransport {
    fn get(&self, url: &str, headers: &BTreeMap<String, String>) -> Result<RemoteResponse, String> {
        self.dispatch("GET", url, headers, None)
    }

    fn post(
        &self,
        url: &str,
        headers: &BTreeMap<String, String>,
        payload: &Value,
    ) -> Result<RemoteResponse, String> {
        self.dispatch("POST", url, headers, Some(payload.clone()))
    }
}

impl ApiTransport {
    fn dispatch(
        &self,
        method: &str,
        url: &str,
        headers: &BTreeMap<String, String>,
        payload: Option<Value>,
    ) -> Result<RemoteResponse, String> {
        let path = url
            .strip_prefix(BASE_URL)
            .ok_or_else(|| format!("unexpected base URL: {url}"))?
            .to_string();
        let router = self.router.clone();
        let method = method.to_string();
        let headers = headers.clone();
        let handle = tokio::runtime::Handle::current();
        tokio::task::block_in_place(move || {
            handle.block_on(async move {
                let mut builder = Request::builder().method(method.as_str()).uri(path);
                for (name, value) in &headers {
                    builder = builder.header(name, value);
                }
                let body = match payload {
                    Some(value) => {
                        Body::from(serde_json::to_vec(&value).map_err(|e| e.to_string())?)
                    }
                    None => Body::empty(),
                };
                let request = builder.body(body).map_err(|error| error.to_string())?;
                let response = router
                    .oneshot(request)
                    .await
                    .map_err(|error| error.to_string())?;
                let status_code = response.status().as_u16();
                let mut response_headers = BTreeMap::new();
                for (name, value) in response.headers() {
                    if let Ok(value) = value.to_str() {
                        response_headers.insert(name.to_string(), value.to_string());
                    }
                }
                let bytes = response
                    .into_body()
                    .collect()
                    .await
                    .map_err(|error| error.to_string())?
                    .to_bytes();
                let body = if bytes.is_empty() {
                    Value::Object(serde_json::Map::new())
                } else {
                    serde_json::from_slice(&bytes).unwrap_or(Value::Object(serde_json::Map::new()))
                };
                Ok(RemoteResponse {
                    status_code,
                    headers: response_headers,
                    body,
                })
            })
        })
    }
}

fn mcp_server(router: Router) -> McpServer {
    struct StaticLoader;
    impl ConfigurationLoader for StaticLoader {
        fn load_json(&self, _path: &str) -> Result<Value, String> {
            Ok(backtest_document())
        }
    }
    let mut context = HarnessToolsContext::new(Boundary::Remote, Arc::new(StaticCredentials));
    context.api_base_url = Some(BASE_URL.to_string());
    context.remote_transport = Some(Arc::new(ApiTransport { router }));
    context.configuration_loader = Arc::new(StaticLoader);
    create_server(context.validate().expect("context")).expect("server")
}

fn backtest_document() -> Value {
    json!({
        "schema_version": 1,
        "shell": "backtest",
        "run_id": "momentum-spy-2024-01",
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-19T21:00:00Z",
        "strategy_instances": [
            {"instance_id": "spy-momentum"}
        ]
    })
}

fn tool_call(server: &McpServer, id: u64, tool: &str, arguments: Value) -> Value {
    let request = json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments}
    });
    server.handle(&request).expect("response")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn http_launch_is_queryable_through_mcp() {
    let router = http_app();
    let server = mcp_server(router.clone());

    let launch = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/backtests/jobs")
                .header(header::AUTHORIZATION, format!("Bearer {BEARER}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(
                    serde_json::to_vec(&backtest_document()).expect("json"),
                ))
                .expect("request"),
        )
        .await
        .expect("launch");
    assert_eq!(launch.status(), StatusCode::ACCEPTED);

    let run = tool_call(
        &server,
        1,
        "get_run",
        json!({"run_id": "momentum-spy-2024-01"}),
    );
    assert_eq!(run["result"]["isError"], false);
    assert_eq!(
        run["result"]["structuredContent"]["run_id"],
        "momentum-spy-2024-01"
    );
    assert_eq!(run["result"]["structuredContent"]["status"], "complete");
    assert_eq!(
        run["result"]["structuredContent"]["strategy_id"],
        "spy-momentum"
    );

    let status = tool_call(
        &server,
        2,
        "get_backtest_status",
        json!({"job_id": "momentum-spy-2024-01"}),
    );
    assert_eq!(status["result"]["structuredContent"]["state"], "succeeded");

    let artifacts = tool_call(
        &server,
        3,
        "list_artifacts",
        json!({"run_id": "momentum-spy-2024-01"}),
    );
    assert_eq!(
        artifacts["result"]["structuredContent"]["artifacts"][0]["name"],
        "backtest.json"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_run_backtest_remote_launches_and_http_reads_it_back() {
    let router = http_app();
    let server = mcp_server(router.clone());

    let launcher = tool_call(
        &server,
        1,
        "run_backtest",
        json!({"config_path": "bt.json"}),
    );
    assert_eq!(launcher["result"]["isError"], false, "{launcher}");
    let payload = &launcher["result"]["structuredContent"];
    assert_eq!(payload["boundary"], "remote");
    assert_eq!(payload["job_id"], "momentum-spy-2024-01");
    assert_eq!(payload["state"], "succeeded");
    assert_eq!(
        payload["location"],
        "/v1/backtests/jobs/momentum-spy-2024-01"
    );

    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/v1/backtests/runs/momentum-spy-2024-01")
                .header(header::AUTHORIZATION, format!("Bearer {BEARER}"))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("get run");
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let row: Value = serde_json::from_slice(&bytes).expect("json");
    assert_eq!(row["run_id"], "momentum-spy-2024-01");
    assert_eq!(row["status"], "complete");
}
