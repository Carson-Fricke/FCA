use mcp::auth::EnvironmentBearerTokenSource;
use mcp::tools::{BacktestSubmitter, Boundary, HarnessToolsContext};
use serde_json::{json, Value};
use std::sync::Arc;

struct Submitter;
impl BacktestSubmitter for Submitter {
    fn submit(&self, _: &str, _: Option<&str>) -> Result<Value, String> {
        Ok(json!({"accepted":true}))
    }
}

#[test]
fn actual_tool_dispatch_validates_inline_configuration_before_submitter() {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.backtest_submitter = Some(Arc::new(Submitter));
    let server = mcp::create_server(context).unwrap();
    for invalid in [false, true] {
        let mut document = json!({"shell":"backtest","run_id":"mcp-parity",
            "data":{"symbols":["SPY"],
                "subscriptions":[{"data_type":"quote","symbols":["SPY"]}]}});
        if invalid {
            document["fees"] = json!([]);
        }
        let request = json!({"jsonrpc":"2.0","id":1,"method":"tools/call",
            "params":{"name":"run_backtest","arguments":{"config":document}}});
        let response = server.handle(&request).unwrap();
        assert_eq!(response["result"]["isError"], json!(invalid), "{response}");
    }
}
