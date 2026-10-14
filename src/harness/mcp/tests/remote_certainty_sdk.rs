use mcp::auth::{bearer_headers, BearerTokenSource, MissingBearerCredentialError};
use mcp::tools::{
    Boundary, HarnessToolsContext, RemoteResponse, RemoteTransport, StudyValidator,
    ToolExecutionError,
};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

struct Token;
impl BearerTokenSource for Token {
    fn bearer_token(&self) -> Option<String> {
        Some("test-token".into())
    }
    fn bearer_headers(&self) -> Result<BTreeMap<String, String>, MissingBearerCredentialError> {
        bearer_headers(Some("test-token"))
    }
}
struct Transport {
    response: Result<RemoteResponse, String>,
}
impl RemoteTransport for Transport {
    fn get(&self, _: &str, _: &BTreeMap<String, String>) -> Result<RemoteResponse, String> {
        panic!("no get")
    }
    fn post(
        &self,
        _: &str,
        _: &BTreeMap<String, String>,
        _: &Value,
    ) -> Result<RemoteResponse, String> {
        self.response.clone()
    }
}

#[test]
fn remote_mutation_loss_is_unknown_and_server_rejections_keep_detail_and_certainty() {
    for (response, certainty, detail) in [
        (
            Err("response lost after submit".into()),
            "unknown_outcome",
            "outcome is unknown",
        ),
        (
            Ok(RemoteResponse {
                status_code: 500,
                headers: BTreeMap::new(),
                body: json!({"error": {"message": "queue response lost"}}),
            }),
            "unknown_outcome",
            "queue response lost",
        ),
        (
            Ok(RemoteResponse {
                status_code: 422,
                headers: BTreeMap::new(),
                body: json!({"error": {"message": "invalid subscriptions"}}),
            }),
            "not_started",
            "invalid subscriptions",
        ),
        (
            Ok(RemoteResponse {
                status_code: 500,
                headers: BTreeMap::new(),
                body: json!({"executionCertainty": "observed_failure", "message": "job failed"}),
            }),
            "observed_failure",
            "job failed",
        ),
        (
            Ok(RemoteResponse {
                status_code: 202,
                headers: BTreeMap::new(),
                body: json!({"state": "running"}),
            }),
            "unknown_outcome",
            "job_id",
        ),
    ] {
        let mut context = HarnessToolsContext::new(Boundary::Remote, Arc::new(Token));
        context.api_base_url = Some("https://harness.example".into());
        context.remote_transport = Some(Arc::new(Transport { response }));
        let server = mcp::create_server(context).unwrap();
        let result = server.handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "run_backtest", "arguments": {"config": {"shell": "backtest", "job_id": "certainty",
                "data": {"symbols": ["SPY"],
                    "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]}}}
        }})).unwrap();
        assert_eq!(result["result"]["isError"], true);
        assert_eq!(
            result["result"]["structuredContent"]["executionCertainty"],
            certainty
        );
        assert!(result["result"]["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains(detail));
    }
}

struct Validator(AtomicUsize);
impl StudyValidator for Validator {
    fn validation_identity(&self) -> Option<String> {
        Some(format!(
            "compiler-{}",
            self.0.fetch_add(1, Ordering::SeqCst)
        ))
    }
    fn sdk_reference(&self, _: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        Ok(json!({"sdkIdentity": "sdk-content-hash", "sdkVersion": "0.1.0", "sdkAbiVersion": 3}))
    }
    fn validate(&self, _: &Value) -> Result<Value, String> {
        unreachable!()
    }
}

#[test]
fn initialize_metadata_is_fresh_and_sdk_catalog_reads_serving_validator() {
    let mut context = HarnessToolsContext::new(Boundary::Local, Arc::new(Token));
    context.study_validator = Some(Arc::new(Validator(AtomicUsize::new(0))));
    let server = mcp::create_server(context).unwrap();
    let catalog = server
        .handle(&json!({"jsonrpc": "2.0", "id": 99, "method": "tools/list"}))
        .unwrap();
    for tool in catalog["result"]["tools"].as_array().unwrap() {
        let expected = matches!(
            tool["name"].as_str().unwrap(),
            "read_sdk_reference"
                | "backtest_config_template"
                | "screener_config_template"
                | "get_job_status"
                | "get_job_result"
                | "list_artifacts"
                | "read_artifact"
        );
        assert_eq!(tool["annotations"]["readOnlyHint"], expected);
    }
    for index in 0..2 {
        let response = server
            .handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
            .unwrap();
        assert_eq!(
            response["result"]["serverInfo"]["validationIdentity"],
            format!("compiler-{index}")
        );
        assert_eq!(
            response["result"]["serverInfo"]["sdkIdentity"],
            "sdk-content-hash"
        );
    }
    let response = server
        .handle(
            &json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "read_sdk_reference", "arguments": {}
            }}),
        )
        .unwrap();
    assert_eq!(
        response["result"]["structuredContent"]["sdkIdentity"],
        "sdk-content-hash"
    );
}

#[test]
fn remote_sdk_guidance_and_initialize_use_remote_reference_identity() {
    let mut context = HarnessToolsContext::new(Boundary::Remote, Arc::new(Token));
    context.api_base_url = Some("https://harness.example".into());
    context.remote_transport = Some(Arc::new(Transport {
        response: Ok(RemoteResponse {
            status_code: 200,
            headers: BTreeMap::new(),
            body: json!({"jsonrpc": "2.0", "id": 1,
            "result": {"isError": false, "structuredContent": {
                "sdkIdentity": "remote-sdk", "sdkVersion": "9.0", "sdkAbiVersion": 8,
                "validationIdentity": "remote-compiler", "content": "remote source guidance"
            }}}),
        }),
    }));
    let server = mcp::create_server(context).unwrap();
    let response = server
        .handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        .unwrap();
    assert_eq!(
        response["result"]["serverInfo"]["sdkIdentity"],
        "remote-sdk"
    );
    assert_eq!(
        response["result"]["serverInfo"]["validationIdentity"],
        "remote-compiler"
    );
    let response = server
        .handle(
            &json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "read_sdk_reference", "arguments": {"topic": "consumer"}
            }}),
        )
        .unwrap();
    assert_eq!(
        response["result"]["structuredContent"]["content"],
        "remote source guidance"
    );
}
