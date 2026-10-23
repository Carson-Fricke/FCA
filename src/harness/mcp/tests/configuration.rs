use mcp::auth::bearer_headers;
use mcp::auth::BearerTokenSource;
use mcp::auth::EnvironmentBearerTokenSource;
use mcp::auth::MissingBearerCredentialError;
use mcp::tools::ports::BacktestSubmitter;
use mcp::tools::ports::Boundary;
use mcp::tools::ports::HarnessToolsContext;
use mcp::tools::ports::RemoteResponse;
use mcp::tools::ports::RemoteTransport;
use serde_json::json;
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::Mutex;

struct Submitter;
impl BacktestSubmitter for Submitter {
    fn submit(&self, _: &str, _: Option<&str>, _: Option<&str>) -> Result<Value, String> {
        Ok(json!({"accepted":true}))
    }
}

#[derive(Default)]
struct RecordingSubmitter {
    job_id: Mutex<Option<String>>,
    document_job_id: Mutex<Option<String>>,
}

impl BacktestSubmitter for RecordingSubmitter {
    fn submit(
        &self,
        config_path: &str,
        _: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String> {
        *self.job_id.lock().unwrap() = job_id.map(str::to_string);
        let text = std::fs::read_to_string(config_path).map_err(|error| error.to_string())?;
        let document: Value = serde_json::from_str(&text).map_err(|error| error.to_string())?;
        *self.document_job_id.lock().unwrap() = document
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        Ok(json!({"accepted": true}))
    }
}

struct Token;
impl BearerTokenSource for Token {
    fn bearer_token(&self) -> Option<String> {
        Some("test-token".into())
    }
    fn bearer_headers(&self) -> Result<BTreeMap<String, String>, MissingBearerCredentialError> {
        bearer_headers(Some("test-token"))
    }
}

#[derive(Default)]
struct CapturingTransport {
    payload: Mutex<Option<Value>>,
}

impl RemoteTransport for CapturingTransport {
    fn get(&self, _: &str, _: &BTreeMap<String, String>) -> Result<RemoteResponse, String> {
        panic!("no get")
    }

    fn post(
        &self,
        _: &str,
        _: &BTreeMap<String, String>,
        payload: &Value,
    ) -> Result<RemoteResponse, String> {
        *self.payload.lock().unwrap() = Some(payload.clone());
        Ok(RemoteResponse {
            status_code: 202,
            headers: BTreeMap::new(),
            body: json!({"job_id": payload["job_id"], "state": "queued"}),
        })
    }
}

fn local_server(submitter: Arc<dyn BacktestSubmitter>) -> mcp::McpServer {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.backtest_submitter = Some(submitter);
    mcp::create_server(context).unwrap()
}

fn backtest_request(arguments: Value) -> Value {
    json!({"jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"run_backtest","arguments":arguments}})
}

fn inline_document() -> Value {
    json!({"shell":"backtest",
        "data":{"symbols":["SPY"],
            "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}})
}

#[test]
fn actual_tool_dispatch_validates_inline_configuration_before_submitter() {
    let server = local_server(Arc::new(Submitter));
    for invalid in [false, true] {
        let mut document = json!({"shell":"backtest","job_id":"mcp-parity",
            "data":{"symbols":["SPY"],
                "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}});
        if invalid {
            document["fees"] = json!([]);
        }
        let request = backtest_request(json!({"config":document}));
        let response = server.handle(&request).unwrap();
        assert_eq!(response["result"]["isError"], json!(invalid), "{response}");
    }
}

#[test]
fn run_backtest_applies_a_provided_job_id_before_validation() {
    let submitter = Arc::new(RecordingSubmitter::default());
    let server = local_server(submitter.clone());
    let response = server
        .handle(&backtest_request(json!({
            "config": inline_document(),
            "job_id": "job-mcp-overlay"
        })))
        .unwrap();
    assert_eq!(response["result"]["isError"], json!(false), "{response}");
    assert_eq!(
        submitter.job_id.lock().unwrap().as_deref(),
        Some("job-mcp-overlay")
    );
    assert_eq!(
        submitter.document_job_id.lock().unwrap().as_deref(),
        Some("job-mcp-overlay")
    );
}

#[test]
fn run_backtest_rejects_a_document_without_job_id() {
    let server = local_server(Arc::new(Submitter));
    let response = server
        .handle(&backtest_request(json!({"config": inline_document()})))
        .unwrap();
    assert_eq!(response["result"]["isError"], json!(true), "{response}");
    let text = response["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("job_id"), "{text}");
}

#[test]
fn run_backtest_applies_a_submitted_job_id_over_the_document() {
    let submitter = Arc::new(RecordingSubmitter::default());
    let server = local_server(submitter.clone());
    let mut document = inline_document();
    document["job_id"] = json!("job-document");
    let response = server
        .handle(&backtest_request(json!({
            "config": document,
            "job_id": "job-submitted"
        })))
        .unwrap();
    assert_eq!(response["result"]["isError"], json!(false), "{response}");
    assert_eq!(
        submitter.job_id.lock().unwrap().as_deref(),
        Some("job-submitted")
    );
    assert_eq!(
        submitter.document_job_id.lock().unwrap().as_deref(),
        Some("job-submitted")
    );
}

#[test]
fn submission_tools_advertise_an_optional_job_id() {
    use mcp::tools::catalog::build_catalog;
    use mcp::tools::TOOL_RUN_BACKTEST;
    use mcp::tools::TOOL_RUN_SCREENER;
    use mcp::tools::TOOL_RUN_STUDY;
    let context = HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    let catalog = build_catalog(context).unwrap();
    for name in [TOOL_RUN_BACKTEST, TOOL_RUN_STUDY, TOOL_RUN_SCREENER] {
        let tool = catalog.resolve(name).unwrap();
        assert_eq!(
            tool.input_schema["properties"]["job_id"],
            json!({"type": "string", "minLength": 1, "maxLength": 128}),
            "{name}"
        );
    }
}

#[test]
fn remote_run_backtest_forwards_the_provided_job_id() {
    let transport = Arc::new(CapturingTransport::default());
    let mut context = HarnessToolsContext::new(Boundary::Remote, Arc::new(Token));
    context.api_base_url = Some("https://harness.example".into());
    context.remote_transport = Some(transport.clone());
    let server = mcp::create_server(context).unwrap();
    let response = server
        .handle(&backtest_request(json!({
            "config": inline_document(),
            "job_id": "job-remote-overlay"
        })))
        .unwrap();
    assert_eq!(response["result"]["isError"], json!(false), "{response}");
    let payload = transport.payload.lock().unwrap().clone().unwrap();
    assert_eq!(payload["job_id"], "job-remote-overlay");
}
