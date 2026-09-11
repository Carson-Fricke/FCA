//! Focused MCP local-mode coverage for the durable query and submitter paths.
//!
//! The server is composed in `local` mode over an in-memory durable store with
//! injected submitter boundaries, so every tool call is exercised without
//! spawning a process or touching the network.

use std::collections::BTreeMap;
use std::sync::Arc;

use mcp::auth::EnvironmentBearerTokenSource;
use mcp::store::{
    artifact_digest, InMemoryJobRepository, InMemoryRunStore, JobRecord, JobRepository, JobState,
    RunCatalogRecord, RunStatus, RunStore,
};
use mcp::tools::{
    BacktestSubmitter, Boundary, HarnessToolsContext, ProcessOutcome, ProcessRunner,
    SweepSubmitter, TOOL_BACKTEST_STATUS, TOOL_GET_RUN, TOOL_LIST_ARTIFACTS, TOOL_RUN_BACKTEST,
    TOOL_RUN_SWEEP,
};
use mcp::{create_server, McpServer};
use serde_json::{json, Value};

struct EchoRunner;

impl ProcessRunner for EchoRunner {
    fn run(&self, argv: &[String]) -> Result<ProcessOutcome, String> {
        Ok(ProcessOutcome {
            returncode: 0,
            stdout: argv.join(" "),
            stderr: String::new(),
        })
    }
}

struct FixedBacktestSubmitter;

impl BacktestSubmitter for FixedBacktestSubmitter {
    fn submit(&self, _config_path: &str, _workspace: Option<&str>) -> Result<Value, String> {
        Ok(json!({
            "boundary": "local-submit",
            "run_id": "run-1",
            "status": "completed",
        }))
    }
}

struct FixedSweepSubmitter;

impl SweepSubmitter for FixedSweepSubmitter {
    fn submit(&self, _spec_path: &str) -> Result<Value, String> {
        Ok(json!({
            "sweep_id": "sweep-1",
            "requested": 2,
            "expanded": 2,
            "duplicates_skipped": 0,
            "run_ids": ["r1", "r2"],
        }))
    }
}

fn local_server(store: Arc<InMemoryRunStore>, jobs: Arc<InMemoryJobRepository>) -> McpServer {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.runner = Some(Arc::new(EchoRunner));
    context.run_store = Some(store);
    context.job_repository = Some(jobs);
    context.backtest_submitter = Some(Arc::new(FixedBacktestSubmitter));
    context.sweep_submitter = Some(Arc::new(FixedSweepSubmitter));
    create_server(context.validate().expect("context")).expect("server")
}

fn call(server: &McpServer, name: &str, arguments: Value) -> Value {
    server
        .handle(&json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments}
        }))
        .expect("response")
}

#[test]
fn local_mode_queries_the_durable_store() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let mut artifacts = BTreeMap::new();
    artifacts.insert("backtest.json".to_string(), b"{}".to_vec());
    store
        .put(
            RunCatalogRecord::new("run-1", RunStatus::Complete, "momentum"),
            artifacts,
        )
        .expect("seed");
    let now = chrono::Utc::now();
    jobs.upsert(JobRecord::new("run-1", JobState::Succeeded, now, now));
    let server = local_server(store, jobs);

    let status = call(&server, TOOL_BACKTEST_STATUS, json!({"job_id": "run-1"}));
    assert_eq!(status["result"]["isError"], false);
    assert_eq!(status["result"]["structuredContent"]["job_id"], "run-1");
    assert_eq!(status["result"]["structuredContent"]["state"], "succeeded");

    let run = call(&server, TOOL_GET_RUN, json!({"run_id": "run-1"}));
    assert_eq!(run["result"]["structuredContent"]["run_id"], "run-1");
    assert_eq!(run["result"]["structuredContent"]["status"], "complete");

    let artifacts = call(&server, TOOL_LIST_ARTIFACTS, json!({"run_id": "run-1"}));
    let entry = &artifacts["result"]["structuredContent"]["artifacts"][0];
    assert_eq!(entry["name"], "backtest.json");
    assert_eq!(entry["uri"], "backtests/run-1/backtest.json");
    assert_eq!(entry["sizeBytes"], 2);
    assert_eq!(entry["sha256"], artifact_digest(b"{}"));
    assert_eq!(entry["sas_url"], Value::Null);
}

#[test]
fn local_run_backtest_uses_the_composed_submitter() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = local_server(store, jobs);
    let response = call(
        &server,
        TOOL_RUN_BACKTEST,
        json!({"config_path": "bt.yaml"}),
    );
    assert_eq!(response["result"]["isError"], false);
    assert_eq!(
        response["result"]["structuredContent"]["boundary"],
        "local-submit"
    );
}

#[test]
fn local_run_sweep_uses_the_composed_submitter() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = local_server(store, jobs);
    let response = call(&server, TOOL_RUN_SWEEP, json!({"spec_path": "spec.json"}));
    assert_eq!(response["result"]["isError"], false);
    assert_eq!(
        response["result"]["structuredContent"]["sweep_id"],
        "sweep-1"
    );
    assert_eq!(response["result"]["structuredContent"]["expanded"], 2);
}

#[test]
fn local_missing_records_are_is_error() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = local_server(store, jobs);
    let response = call(&server, TOOL_GET_RUN, json!({"run_id": "missing"}));
    assert_eq!(response["result"]["isError"], true);
}
