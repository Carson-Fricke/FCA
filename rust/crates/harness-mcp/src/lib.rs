//! JSON-RPC 2.0 / MCP server for the quant-harness Rust port.
//!
//! This crate ports `harness.mcp` onto Rust: the capability-filtered tool
//! catalog, the pure JSON-RPC dispatch, the bearer credential resolution, and
//! the newline-delimited stdio transport. Every external boundary (process
//! runner, remote HTTP transport, sweep submitter, configuration loader) is
//! injected, so the server is exercised without spawning processes or
//! touching the network.

pub mod auth;
pub mod server;
pub mod tools;
pub mod transport;

pub use server::{
    create_server, create_server_named, error_response, McpServer, PROTOCOL_VERSION, SERVER_NAME,
    SERVER_VERSION,
};
pub use tools::{build_catalog, HarnessTools, HarnessToolsContext, ToolCapabilities, ToolCatalog};
pub use transport::{decode_message, encode_message, serve_stdio, InProcessTransport};

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    use serde_json::{json, Value};

    use super::auth::{bearer_headers, EnvironmentBearerTokenSource};
    use super::server::{
        INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, PROTOCOL_VERSION,
    };
    use super::tools::{
        Boundary, ConfigurationLoader, ContextError, HarnessToolsContext, ProcessOutcome,
        ProcessRunner, RemoteResponse, RemoteTransport, SweepSubmitter, TOOL_BACKTEST_STATUS,
        TOOL_GET_RUN, TOOL_LIST_ARTIFACTS, TOOL_RUN_BACKTEST, TOOL_RUN_SWEEP,
    };
    use super::transport::{decode_message, serve_stdio};
    use super::{create_server, InProcessTransport};

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

    struct FailingRunner;

    impl ProcessRunner for FailingRunner {
        fn run(&self, _argv: &[String]) -> Result<ProcessOutcome, String> {
            Ok(ProcessOutcome {
                returncode: 3,
                stdout: "partial".to_string(),
                stderr: "boom".to_string(),
            })
        }
    }

    #[derive(Default)]
    struct RecordingTransport {
        gets: Mutex<Vec<String>>,
        posts: Mutex<Vec<String>>,
    }

    impl RemoteTransport for RecordingTransport {
        fn get(
            &self,
            url: &str,
            _headers: &BTreeMap<String, String>,
        ) -> Result<RemoteResponse, String> {
            self.gets.lock().expect("gets").push(url.to_string());
            Ok(RemoteResponse {
                status_code: 200,
                headers: BTreeMap::new(),
                body: json!({"run_id": "run-1", "status": "complete"}),
            })
        }

        fn post(
            &self,
            url: &str,
            headers: &BTreeMap<String, String>,
            _payload: &Value,
        ) -> Result<RemoteResponse, String> {
            assert!(headers.contains_key("Authorization"));
            self.posts.lock().expect("posts").push(url.to_string());
            let mut headers = BTreeMap::new();
            headers.insert(
                "Location".to_string(),
                "/v1/backtests/jobs/run-1".to_string(),
            );
            Ok(RemoteResponse {
                status_code: 202,
                headers,
                body: json!({"job_id": "run-1", "state": "queued"}),
            })
        }
    }

    struct FixedSubmitter;

    impl SweepSubmitter for FixedSubmitter {
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

    fn local_server(runner: Arc<dyn ProcessRunner>) -> super::McpServer {
        let mut context =
            HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
        context.runner = Some(runner);
        context.sweep_submitter = Some(Arc::new(FixedSubmitter));
        create_server(context.validate().expect("context")).expect("server")
    }

    fn remote_context(transport: Arc<dyn RemoteTransport>) -> HarnessToolsContext {
        let mut context =
            HarnessToolsContext::new(Boundary::Remote, Arc::new(StaticTokenSource));
        context.api_base_url = Some("https://api.example.test".to_string());
        context.remote_transport = Some(transport);
        context.configuration_loader = Arc::new(StaticLoader);
        context
    }

    struct StaticTokenSource;

    impl super::auth::BearerTokenSource for StaticTokenSource {
        fn bearer_token(&self) -> Option<String> {
            Some("test-token".to_string())
        }

        fn bearer_headers(
            &self,
        ) -> Result<BTreeMap<String, String>, super::auth::MissingBearerCredentialError> {
            bearer_headers(Some("test-token"))
        }
    }

    struct StaticLoader;

    impl ConfigurationLoader for StaticLoader {
        fn load_json(&self, _path: &str) -> Result<Value, String> {
            Ok(json!({"shell": "backtest", "run_id": "run-1"}))
        }
    }

    fn call(server: &super::McpServer, request: Value) -> Value {
        server.handle(&request).expect("response")
    }

    #[test]
    fn initialize_reports_tools_capability() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(
            &server,
            json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        );
        assert_eq!(response["result"]["protocolVersion"], PROTOCOL_VERSION);
        assert_eq!(response["result"]["serverInfo"]["name"], "harness-mcp");
        assert_eq!(response["result"]["capabilities"]["tools"]["listChanged"], false);
    }

    #[test]
    fn ping_and_tools_list_work() {
        let server = local_server(Arc::new(EchoRunner));
        assert_eq!(
            call(&server, json!({"jsonrpc": "2.0", "id": 1, "method": "ping"})),
            json!({"jsonrpc": "2.0", "id": 1, "result": {}})
        );
        let response = call(
            &server,
            json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        );
        let names: Vec<&str> = response["result"]["tools"]
            .as_array()
            .expect("tools")
            .iter()
            .map(|tool| tool["name"].as_str().expect("name"))
            .collect();
        assert_eq!(
            names,
            vec![
                TOOL_RUN_BACKTEST,
                TOOL_BACKTEST_STATUS,
                TOOL_GET_RUN,
                TOOL_LIST_ARTIFACTS,
                TOOL_RUN_SWEEP
            ]
        );
    }

    #[test]
    fn unknown_method_is_method_not_found() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(
            &server,
            json!({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"}),
        );
        assert_eq!(response["error"]["code"], METHOD_NOT_FOUND);
    }

    #[test]
    fn non_object_request_is_invalid_request() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(&server, json!([1, 2, 3]));
        assert_eq!(response["error"]["code"], INVALID_REQUEST);
        assert_eq!(response["id"], Value::Null);
    }

    #[test]
    fn incorrect_jsonrpc_version_is_invalid_request() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(&server, json!({"jsonrpc": "1.0", "id": 5, "method": "ping"}));
        assert_eq!(response["error"]["code"], INVALID_REQUEST);
        assert_eq!(response["id"], 5);
    }

    #[test]
    fn notifications_produce_no_response() {
        let server = local_server(Arc::new(EchoRunner));
        assert!(server
            .handle(&json!({"jsonrpc": "2.0", "method": "ping"}))
            .is_none());
    }

    #[test]
    fn tools_call_local_backtest_returns_structured_content() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": "bt.yaml"}}
            }),
        );
        assert_eq!(response["result"]["isError"], false);
        assert_eq!(response["result"]["structuredContent"]["boundary"], "local");
        assert_eq!(response["result"]["structuredContent"]["exit_code"], 0);
        assert!(response["result"]["content"][0]["text"].is_string());
    }

    #[test]
    fn failing_process_is_reported_as_is_error_with_payload() {
        let server = local_server(Arc::new(FailingRunner));
        let response = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": "bt.yaml"}}
            }),
        );
        assert_eq!(response["result"]["isError"], true);
        assert_eq!(response["result"]["structuredContent"]["exit_code"], 3);
    }

    #[test]
    fn invalid_arguments_are_invalid_params() {
        let server = local_server(Arc::new(EchoRunner));
        let missing = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_RUN_BACKTEST, "arguments": {}}
            }),
        );
        assert_eq!(missing["error"]["code"], INVALID_PARAMS);

        let unexpected = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": "x", "surprise": 1}}
            }),
        );
        assert_eq!(unexpected["error"]["code"], INVALID_PARAMS);
    }

    #[test]
    fn unknown_tool_is_invalid_params() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}}
            }),
        );
        assert_eq!(response["error"]["code"], INVALID_PARAMS);
    }

    #[test]
    fn run_sweep_uses_the_injected_submitter() {
        let server = local_server(Arc::new(EchoRunner));
        let response = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_RUN_SWEEP, "arguments": {"spec_path": "spec.json"}}
            }),
        );
        assert_eq!(response["result"]["structuredContent"]["sweep_id"], "sweep-1");
    }

    #[test]
    fn remote_mode_queries_and_launches_through_the_transport() {
        let transport = Arc::new(RecordingTransport::default());
        let server = create_server(remote_context(transport.clone()).validate().expect("ctx"))
            .expect("server");
        let get = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL_GET_RUN, "arguments": {"run_id": "run-1"}}
            }),
        );
        assert_eq!(get["result"]["structuredContent"]["run_id"], "run-1");
        assert_eq!(
            transport.gets.lock().expect("gets")[0],
            "https://api.example.test/v1/backtests/runs/run-1"
        );

        let launch = call(
            &server,
            json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": "bt.json"}}
            }),
        );
        assert_eq!(launch["result"]["structuredContent"]["job_id"], "run-1");
        assert_eq!(launch["result"]["structuredContent"]["location"], "/v1/backtests/jobs/run-1");
    }

    #[test]
    fn stdio_loop_answers_requests_and_skips_notifications() {
        let server = local_server(Arc::new(EchoRunner));
        let input = concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}\n",
            "not json\n",
            "{\"jsonrpc\":\"2.0\",\"method\":\"ping\"}\n",
        );
        let mut output = Vec::new();
        let mut diagnostics = Vec::new();
        let answered = serve_stdio(
            &server,
            std::io::Cursor::new(input),
            &mut output,
            &mut diagnostics,
        )
        .expect("serve");
        assert_eq!(answered, 2);
        let lines: Vec<&str> = std::str::from_utf8(&output).expect("utf8").trim().split('\n').collect();
        assert_eq!(lines.len(), 2);
        let ping: Value = decode_message(lines[0]).expect("json");
        assert_eq!(ping["result"], json!({}));
        let parse_error: Value = decode_message(lines[1]).expect("json");
        assert_eq!(parse_error["error"]["code"], PARSE_ERROR);
        assert_eq!(parse_error["id"], Value::Null);
    }

    #[test]
    fn in_process_transport_dispatches_directly() {
        let transport = InProcessTransport::new(local_server(Arc::new(EchoRunner)));
        let response = transport
            .call(&json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
            .expect("response");
        assert_eq!(response["result"], json!({}));
    }

    #[test]
    fn remote_mode_without_transport_fails_validation() {
        let mut context =
            HarnessToolsContext::new(Boundary::Remote, Arc::new(EnvironmentBearerTokenSource));
        context.api_base_url = Some("https://api.example.test".to_string());
        let result = context.validate();
        assert_eq!(
            result.err(),
            Some(ContextError(
                "remote execution requires an https api_base_url and a RemoteTransport".to_string()
            ))
        );
    }

    #[test]
    fn bearer_headers_are_used_for_remote_calls() {
        assert!(bearer_headers(Some("t")).expect("headers").contains_key("Authorization"));
    }
}
