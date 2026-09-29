//! MCP study composition: an authored bar study replays the lake's bars.
//!
//! The test starts one local fake of the frozen market-lake routes, points
//! `LAKE_API_URL` / `LAKE_API__BEARER_TOKEN` at it, and drives the production
//! MCP local composition (`compose_local_server`). The submitted
//! `run_study` tool call runs through the background `NativeStudySubmitter`,
//! which clamps the window to bar coverage, ensures and reads `data_type=bars`
//! with the configured interval, replays the bars through the real engine, and
//! persists `study.json` plus `metrics.json`. No network and no lake files are
//! touched.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use api::http::mcp::compose_local_server;
use api::http::store::{
    InMemoryJobRepository, InMemoryRunStore, JobRepository, JobState, RunStore,
};
use harness_lakeclient::testing::{bars_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer};
use mcp::server::McpServer;
use mcp::tools::TOOL_RUN_STUDY;
use serde_json::{json, Value};

const TOKEN: &str = "study-composition-token";

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
        "harness-api-study-source-{}-{name}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

/// Three one-minute SPY bars inside the default study window.
fn bar_rows() -> Vec<FakeBarRow> {
    use backtest::backtest::errors::ONE_DAY_NS;
    use marketdata::Date;

    let date = Date::from_ymd(2024, 1, 2).expect("date");
    let base = i64::from(date.days()) * ONE_DAY_NS + (14 * 3_600 + 30 * 60) * 1_000_000_000;
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

fn write_study_config(directory: &Path) -> PathBuf {
    let source = include_str!("fixtures/parity_study.rs");
    let digest = backtest::sha256::digest_hex(source.replace("\r\n", "\n").as_bytes());
    let document = json!({
        "schema_version": 1,
        "shell": "study",
        "run_id": "mcp-bar-study",
        "seed": 7,
        "data": {"symbols": ["SPY"], "bar_interval": "1min"},
        "strategy_instances": [{
            "instance_id": "bar-research",
            "implementation": "jit",
            "bundle_hash": digest,
            "capital_fraction": "1.0",
            "symbols": ["SPY"],
            "parameters": {"source": source}
        }]
    });
    let path = directory.join("study.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&document).expect("serialize config"),
    )
    .expect("write config");
    path
}

fn poll_terminal(jobs: &Arc<dyn JobRepository>, job_id: &str) -> JobState {
    for _ in 0..600 {
        if let Some(record) = jobs.get(job_id) {
            if matches!(record.state, JobState::Succeeded | JobState::Failed) {
                if record.state == JobState::Failed {
                    eprintln!("job {job_id} failed: {:?}", record.error_message);
                }
                return record.state;
            }
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    panic!("job '{job_id}' did not reach a terminal state");
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
fn a_composed_bar_study_replays_bars_and_persists_its_artifacts() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies_by_type: BTreeMap::from([(
            "bars".to_string(),
            bars_stream("SPY", &bar_rows()),
        )]),
        ..FakeLakeConfig::new(TOKEN)
    });
    let _env = LakeEnv::set(server.base_url(), TOKEN);
    let directory = temp_dir("composition");
    let config_path = write_study_config(&directory);

    let run_store: Arc<dyn RunStore> = Arc::new(InMemoryRunStore::new());
    let jobs: Arc<dyn JobRepository> = Arc::new(InMemoryJobRepository::new());
    let mcp_server = compose_local_server(Arc::clone(&run_store), Arc::clone(&jobs))
        .expect("composed MCP server");
    let workspace = directory.join("mcp-out");
    let response = call_tool(
        &mcp_server,
        TOOL_RUN_STUDY,
        json!({
            "config_path": config_path.to_str().expect("config path"),
            "workspace": workspace.to_str().expect("workspace"),
        }),
    );
    assert_eq!(response["result"]["isError"], false, "{response}");
    let run_id = response["result"]["structuredContent"]["study_id"]
        .as_str()
        .expect("study id")
        .to_string();
    assert_eq!(poll_terminal(&jobs, &run_id), JobState::Succeeded);

    // The catalog row carries the authored metric and the run stores the
    // canonical `study.json` plus the derived `metrics.json`.
    let record = run_store.get(&run_id).expect("run record");
    assert_eq!(
        record
            .metrics
            .get("bar_observations")
            .and_then(|value| value.parse::<f64>().ok()),
        Some(3.0)
    );
    let stored = run_store.fetch(&run_id).expect("stored run");
    assert!(
        stored.artifacts.contains_key("study.json"),
        "artifacts: {:?}",
        stored.artifacts.keys().collect::<Vec<_>>()
    );
    let metrics: Value = serde_json::from_slice(
        stored
            .artifacts
            .get("metrics.json")
            .expect("metrics artifact"),
    )
    .expect("metrics json");
    assert_eq!(metrics["custom"]["bar_observations"], "3");

    // The lake traffic requested bars with the configured interval.
    let read_query = server
        .requests()
        .iter()
        .find(|request| request.path == "/v1/read")
        .expect("read request")
        .query
        .clone();
    assert_eq!(
        read_query.get("data_type").map(String::as_str),
        Some("bars")
    );
    assert_eq!(read_query.get("interval").map(String::as_str), Some("1min"));

    let _ = std::fs::remove_dir_all(&directory);
}
