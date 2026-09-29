//! Real-source composition: the native screening runners read the lake routes.
//!
//! The test starts one local fake of the frozen market-lake routes, points
//! `LAKE_API_URL` / `LAKE_API__BEARER_TOKEN` at it, and then drives the
//! production compositions: the `screen-job` runner and the MCP local
//! submitter composed by [`api::http::mcp::compose_local_server`]. The fake
//! answers the read with `500`, so the runs fail before any JIT compile and
//! the test observes the lake traffic instead of a compile. No network and no
//! lake files are touched.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use api::http::mcp::compose_local_server;
use api::http::store::{
    InMemoryJobRepository, InMemoryRunStore, JobRepository, JobState, RunStatus, RunStore,
};
use api::{NativeScreenJobRunner, ScreenJobArgs, ScreenJobRunner};
use harness_lakeclient::testing::{FakeLakeConfig, FakeLakeServer};
use mcp::server::McpServer;
use mcp::tools::TOOL_RUN_SCREENER;
use serde_json::{json, Value};

const TOKEN: &str = "composition-token";

/// Set the lake environment for one test process and restore it on drop.
struct LakeEnv {
    url: Option<String>,
    base: Option<String>,
    token: Option<String>,
}

impl LakeEnv {
    fn set(url: &str, token: &str) -> Self {
        let guard = Self {
            url: std::env::var("LAKE_API_URL").ok(),
            base: std::env::var("LAKE_API_BASE_URL").ok(),
            token: std::env::var("LAKE_API__BEARER_TOKEN").ok(),
        };
        std::env::set_var("LAKE_API_URL", url);
        std::env::set_var("LAKE_API__BEARER_TOKEN", token);
        guard
    }

    fn restore(name: &str, value: Option<&String>) {
        match value {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
    }
}

impl Drop for LakeEnv {
    fn drop(&mut self) {
        Self::restore("LAKE_API_URL", self.url.as_ref());
        Self::restore("LAKE_API_BASE_URL", self.base.as_ref());
        Self::restore("LAKE_API__BEARER_TOKEN", self.token.as_ref());
    }
}

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-api-lake-source-{}-{name}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

fn screen_document() -> Value {
    let source = "fn main() {}\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "composition-screen",
        "as_of": "2024-01-19",
        "bar_interval": "1d",
        "lookback_days": 20,
        "rank_metric": "close",
        "universe_revision": "sp500-2024-01",
        "source": source,
        "source_sha256": digest,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "a".repeat(64)),
        "definition_hash": "b".repeat(64),
        "parameters": {},
    })
}

fn write_screen_config(directory: &Path) -> PathBuf {
    let path = directory.join("screen.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&screen_document()).expect("serialize config"),
    )
    .expect("write config");
    path
}

fn fake_lake() -> FakeLakeServer {
    FakeLakeServer::start(FakeLakeConfig {
        universe_body: json!({"as_of": "2024-01-19", "symbols": ["SPY"]}),
        facts_body: json!({
            "as_of": "2024-01-19",
            "facts": {"SPY": {"sector": "etf"}}
        }),
        read_bodies: vec![(
            500,
            serde_json::to_vec(&json!({
                "error": {"code": "storage_unavailable", "message": "the lake read failed"}
            }))
            .expect("error body"),
        )],
        ..FakeLakeConfig::new(TOKEN)
    })
}

fn poll_terminal(jobs: &Arc<dyn JobRepository>, job_id: &str) -> JobState {
    for _ in 0..400 {
        if let Some(record) = jobs.get(job_id) {
            if matches!(record.state, JobState::Succeeded | JobState::Failed) {
                return record.state;
            }
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    panic!("job '{job_id}' did not reach a terminal state");
}

fn request_paths(server: &FakeLakeServer) -> Vec<String> {
    server
        .requests()
        .iter()
        .map(|request| request.path.clone())
        .collect()
}

fn call_tool(server: &McpServer, name: &str, arguments: Value) -> Value {
    server
        .handle(&json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments}
        }))
        .expect("mcp response")
}

#[test]
fn screening_compositions_read_the_market_lake_routes() {
    let server = fake_lake();
    let _env = LakeEnv::set(server.base_url(), TOKEN);
    let directory = temp_dir("composition");
    let config_path = write_screen_config(&directory);

    // The CLI screen runner composes the real source from the lake
    // environment and reads the reference, ensure, and read routes.
    let summary = NativeScreenJobRunner
        .run(&ScreenJobArgs {
            config: Some(config_path.clone()),
            output_dir: Some(directory.join("cli-out")),
        })
        .expect("the lake failure records a failed run, not a hard error");
    assert_eq!(summary.status, "failed");
    let reason = summary.reason.expect("reason");
    assert!(
        reason.contains("HTTP 500"),
        "the lake failure must surface: {reason}"
    );

    let paths = request_paths(&server);
    assert!(
        paths.contains(&"/v1/reference/universe".to_string()),
        "{paths:?}"
    );
    assert!(
        paths.contains(&"/v1/catalog/ensure".to_string()),
        "{paths:?}"
    );
    assert!(paths.contains(&"/v1/read".to_string()), "{paths:?}");
    for request in server.requests() {
        assert_eq!(
            request.authorization.as_deref(),
            Some("Bearer composition-token"),
            "{request:?}"
        );
    }
    let ensure_body: Value = serde_json::from_slice(
        server
            .requests()
            .iter()
            .find(|request| request.path == "/v1/catalog/ensure")
            .and_then(|request| request.body.as_deref())
            .expect("ensure body"),
    )
    .expect("ensure json");
    assert_eq!(ensure_body["asset_class"], "equity");
    assert_eq!(ensure_body["data_type"], "bars");
    assert_eq!(ensure_body["symbol"], "SPY");
    assert_eq!(ensure_body["interval"], "1d");
    let read_query = server
        .requests()
        .iter()
        .find(|request| request.path == "/v1/read")
        .expect("read request")
        .query
        .clone();
    assert_eq!(read_query.get("symbol").map(String::as_str), Some("SPY"));
    assert_eq!(read_query.get("interval").map(String::as_str), Some("1d"));

    // The MCP local composition injects the same native screening runner into
    // its submitter and observes the same lake traffic.
    let run_store: Arc<dyn RunStore> = Arc::new(InMemoryRunStore::new());
    let jobs: Arc<dyn JobRepository> = Arc::new(InMemoryJobRepository::new());
    let mcp_server = compose_local_server(Arc::clone(&run_store), Arc::clone(&jobs))
        .expect("composed MCP server");
    let workspace = directory.join("mcp-out");
    let response = call_tool(
        &mcp_server,
        TOOL_RUN_SCREENER,
        json!({
            "config_path": config_path.to_str().expect("config path"),
            "workspace": workspace.to_str().expect("workspace"),
        }),
    );
    assert_eq!(response["result"]["isError"], false, "{response}");
    let run_id = response["result"]["structuredContent"]["run_id"]
        .as_str()
        .expect("run id")
        .to_string();
    assert_eq!(poll_terminal(&jobs, &run_id), JobState::Failed);

    let job = jobs.get(&run_id).expect("job record");
    assert!(
        job.error_message
            .as_deref()
            .is_some_and(|message| message.contains("HTTP 500")),
        "the MCP submitter must surface the lake failure: {job:?}"
    );
    let record = run_store.get(&run_id).expect("run record");
    assert_eq!(record.status, RunStatus::Failed);
    let stored = run_store.fetch(&run_id).expect("stored run");
    assert!(
        stored.artifacts.contains_key("screen.json"),
        "artifacts: {:?}",
        stored.artifacts.keys().collect::<Vec<_>>()
    );

    let paths = request_paths(&server);
    assert!(
        paths.contains(&"/v1/reference/universe".to_string()),
        "{paths:?}"
    );
    assert!(paths.contains(&"/v1/read".to_string()), "{paths:?}");

    let _ = std::fs::remove_dir_all(&directory);
}
