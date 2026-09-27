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
    TOOL_BACKTEST_CONFIG_TEMPLATE, TOOL_BACKTEST_STATUS, TOOL_GET_RUN, TOOL_LIST_ARTIFACTS,
    TOOL_READ_ARTIFACT, TOOL_RUN_BACKTEST,
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

struct FailingBacktestSubmitter;

impl BacktestSubmitter for FailingBacktestSubmitter {
    fn submit(&self, _config_path: &str, _workspace: Option<&str>) -> Result<Value, String> {
        Err("could not read backtest config 'examples/backtest.yaml': \
             No such file or directory"
            .to_string())
    }
}

struct RecordingBacktestSubmitter {
    seen: Arc<std::sync::Mutex<Vec<String>>>,
}

impl BacktestSubmitter for RecordingBacktestSubmitter {
    fn submit(&self, config_path: &str, _workspace: Option<&str>) -> Result<Value, String> {
        let text = std::fs::read_to_string(config_path).unwrap_or_default();
        self.seen.lock().expect("recording lock").push(text);
        Ok(json!({
            "boundary": "local-submit",
            "run_id": "inline-run",
            "state": "running",
        }))
    }
}

/// Async test double mirroring `NativeBacktestSubmitter`: returns immediately in
/// `running` state and finishes the run on a detached worker.
struct AsyncBacktestSubmitter {
    run_store: Arc<InMemoryRunStore>,
    jobs: Arc<InMemoryJobRepository>,
}

impl AsyncBacktestSubmitter {
    fn new(run_store: Arc<InMemoryRunStore>, jobs: Arc<InMemoryJobRepository>) -> Self {
        Self { run_store, jobs }
    }
}

impl BacktestSubmitter for AsyncBacktestSubmitter {
    fn submit(&self, _config_path: &str, _workspace: Option<&str>) -> Result<Value, String> {
        let run_id = "run-async".to_string();
        let started = chrono::Utc::now();
        self.jobs
            .upsert(JobRecord::new(&run_id, JobState::Running, started, started));
        let run_store = self.run_store.clone();
        let jobs = self.jobs.clone();
        let background_run_id = run_id.clone();
        std::thread::spawn(move || {
            let mut record =
                RunCatalogRecord::new(&background_run_id, RunStatus::Complete, "spy-momentum");
            record.metrics = BTreeMap::from([
                ("strategy_id".to_string(), "spy-momentum".to_string()),
                ("total_return".to_string(), "0.25".to_string()),
                ("max_drawdown".to_string(), "0.05".to_string()),
                ("cagr".to_string(), "1.5000000000".to_string()),
                ("fill_count".to_string(), "3".to_string()),
                ("fees_total".to_string(), "0.03".to_string()),
            ]);
            let mut artifacts = BTreeMap::new();
            artifacts.insert(
                "backtest.json".to_string(),
                br#"{"run_id":"run-async"}"#.to_vec(),
            );
            artifacts.insert(
                "metrics.json".to_string(),
                br#"{"equity":{"total_return":"0.25"}}"#.to_vec(),
            );
            artifacts.insert("report.html".to_string(), b"<html>report</html>".to_vec());
            artifacts.insert(
                "artifacts/equity.parquet".to_string(),
                vec![0xff, 0x00, 0x01, 0x02],
            );
            let _ = run_store.put(record, artifacts);
            jobs.upsert(JobRecord::new(
                &background_run_id,
                JobState::Succeeded,
                started,
                chrono::Utc::now(),
            ));
        });
        Ok(json!({
            "boundary": "local",
            "run_id": run_id,
            "state": "running",
        }))
    }
}

fn async_local_server(store: Arc<InMemoryRunStore>, jobs: Arc<InMemoryJobRepository>) -> McpServer {
    let submitter = Arc::new(AsyncBacktestSubmitter::new(store.clone(), jobs.clone()));
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.run_store = Some(store);
    context.job_repository = Some(jobs);
    context.backtest_submitter = Some(submitter);
    create_server(context.validate().expect("context")).expect("server")
}

fn wait_for_terminal(jobs: &InMemoryJobRepository, job_id: &str) -> JobState {
    for _ in 0..500 {
        if let Some(record) = jobs.get(job_id) {
            if matches!(record.state, JobState::Succeeded | JobState::Failed) {
                return record.state;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    panic!("job '{job_id}' did not reach a terminal state");
}

fn local_server(store: Arc<InMemoryRunStore>, jobs: Arc<InMemoryJobRepository>) -> McpServer {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.runner = Some(Arc::new(EchoRunner));
    context.run_store = Some(store);
    context.job_repository = Some(jobs);
    context.backtest_submitter = Some(Arc::new(FixedBacktestSubmitter));
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
fn local_run_backtest_surfaces_the_submitter_error() {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.runner = Some(Arc::new(EchoRunner));
    context.backtest_submitter = Some(Arc::new(FailingBacktestSubmitter));
    let server = create_server(context.validate().expect("context")).expect("server");
    let response = call(
        &server,
        TOOL_RUN_BACKTEST,
        json!({"config_path": "examples/backtest.yaml"}),
    );
    assert_eq!(response["result"]["isError"], true);
    let text = response["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_default();
    assert!(
        text.contains("could not read backtest config 'examples/backtest.yaml'"),
        "unexpected error text: {text}"
    );
}

#[test]
fn local_run_backtest_accepts_an_inline_config() {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.runner = Some(Arc::new(EchoRunner));
    let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
    context.backtest_submitter = Some(Arc::new(RecordingBacktestSubmitter { seen: seen.clone() }));
    let server = create_server(context.validate().expect("context")).expect("server");

    let response = call(
        &server,
        TOOL_RUN_BACKTEST,
        json!({"config": {"schema_version": 1, "shell": "backtest", "run_id": "inline-run"}}),
    );

    assert_eq!(response["result"]["isError"], false);
    assert_eq!(
        response["result"]["structuredContent"]["run_id"],
        "inline-run"
    );
    let recorded = seen.lock().expect("recording lock");
    assert_eq!(recorded.len(), 1);
    let document: Value = serde_json::from_str(&recorded[0]).expect("inline config is JSON");
    assert_eq!(document["run_id"], "inline-run");
}

#[test]
fn local_backtest_config_template_is_published() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = local_server(store, jobs);
    let response = call(&server, TOOL_BACKTEST_CONFIG_TEMPLATE, json!({}));

    assert_eq!(response["result"]["isError"], false);
    let template = response["result"]["structuredContent"]["template"]
        .as_str()
        .unwrap_or_default();
    assert!(template.contains("shell: backtest"), "template: {template}");
}

#[test]
fn local_missing_records_are_is_error() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = local_server(store, jobs);
    let response = call(&server, TOOL_GET_RUN, json!({"run_id": "missing"}));
    assert_eq!(response["result"]["isError"], true);
}

#[test]
fn local_async_run_backtest_completes_in_the_background() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = async_local_server(store, jobs.clone());

    let response = call(
        &server,
        TOOL_RUN_BACKTEST,
        json!({"config_path": "bt.yaml"}),
    );
    assert_eq!(response["result"]["isError"], false);
    assert_eq!(response["result"]["structuredContent"]["boundary"], "local");
    assert_eq!(response["result"]["structuredContent"]["state"], "running");
    assert_eq!(
        response["result"]["structuredContent"]["run_id"],
        "run-async"
    );

    assert_eq!(wait_for_terminal(&jobs, "run-async"), JobState::Succeeded);

    let status = call(
        &server,
        TOOL_BACKTEST_STATUS,
        json!({"job_id": "run-async"}),
    );
    assert_eq!(status["result"]["isError"], false);
    assert_eq!(status["result"]["structuredContent"]["state"], "succeeded");

    let run = call(&server, TOOL_GET_RUN, json!({"run_id": "run-async"}));
    assert_eq!(run["result"]["structuredContent"]["status"], "complete");
    let metrics = &run["result"]["structuredContent"]["metrics"];
    assert_eq!(metrics["strategy_id"], "spy-momentum");
    assert_eq!(metrics["total_return"], "0.25");
    assert_eq!(metrics["max_drawdown"], "0.05");
    assert_eq!(metrics["cagr"], "1.5000000000");
    assert_eq!(metrics["fill_count"], "3");
    assert_eq!(metrics["fees_total"], "0.03");

    let artifacts = call(&server, TOOL_LIST_ARTIFACTS, json!({"run_id": "run-async"}));
    let names: Vec<&str> = artifacts["result"]["structuredContent"]["artifacts"]
        .as_array()
        .expect("artifact array")
        .iter()
        .filter_map(|entry| entry["name"].as_str())
        .collect();
    assert!(names.contains(&"backtest.json"));
    assert!(names.contains(&"metrics.json"));
    assert!(names.contains(&"report.html"));
    assert!(names.contains(&"artifacts/equity.parquet"));
}

#[test]
fn local_read_artifact_returns_utf8_for_text_and_hex_for_bytes() {
    let store = Arc::new(InMemoryRunStore::new());
    let jobs = Arc::new(InMemoryJobRepository::new());
    let server = async_local_server(store, jobs.clone());
    call(
        &server,
        TOOL_RUN_BACKTEST,
        json!({"config_path": "bt.yaml"}),
    );
    assert_eq!(wait_for_terminal(&jobs, "run-async"), JobState::Succeeded);

    let text = call(
        &server,
        TOOL_READ_ARTIFACT,
        json!({"run_id": "run-async", "name": "metrics.json"}),
    );
    assert_eq!(text["result"]["isError"], false);
    assert_eq!(text["result"]["structuredContent"]["encoding"], "utf-8");
    assert_eq!(
        text["result"]["structuredContent"]["content"],
        r#"{"equity":{"total_return":"0.25"}}"#
    );

    let bytes = call(
        &server,
        TOOL_READ_ARTIFACT,
        json!({"run_id": "run-async", "name": "artifacts/equity.parquet"}),
    );
    assert_eq!(bytes["result"]["isError"], false);
    assert_eq!(bytes["result"]["structuredContent"]["encoding"], "hex");
    assert_eq!(bytes["result"]["structuredContent"]["content"], "ff000102");
}

#[test]
fn local_read_artifact_reports_missing_artifacts() {
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
    let server = local_server(store, jobs);
    let response = call(
        &server,
        TOOL_READ_ARTIFACT,
        json!({"run_id": "run-1", "name": "missing.json"}),
    );
    assert_eq!(response["result"]["isError"], true);
}
