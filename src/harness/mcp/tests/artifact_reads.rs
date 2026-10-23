//! Remote artifact inspection uses the authenticated canonical job route.
use mcp::auth::bearer_headers;
use mcp::auth::BearerTokenSource;
use mcp::auth::MissingBearerCredentialError;
use mcp::build_catalog;
use mcp::tools::ports::Boundary;
use mcp::tools::ports::HarnessToolsContext;
use mcp::tools::ports::RemoteResponse;
use mcp::tools::ports::RemoteTransport;
use serde_json::json;
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::Arc;
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
    mismatch: bool,
}
impl RemoteTransport for Transport {
    fn get(&self, url: &str, headers: &BTreeMap<String, String>) -> Result<RemoteResponse, String> {
        assert_eq!(url, "https://harness.example/v1/jobs/job-output/artifacts/exploratory_outputs.json?representation=json");
        assert_eq!(headers["Authorization"], "Bearer test-token");
        Ok(RemoteResponse {
            status_code: 200,
            headers: BTreeMap::new(),
            body: json!({"job_id":if self.mismatch {"another-job"} else {"job-output"},"name":"exploratory_outputs.json","encoding":"utf-8","content":"{\"programs\":{}}"}),
        })
    }
    fn post(
        &self,
        _: &str,
        _: &BTreeMap<String, String>,
        _: &Value,
    ) -> Result<RemoteResponse, String> {
        panic!("reads never mutate")
    }
}
#[test]
fn remote_artifacts_return_canonical_content_and_reject_other_job_attribution() {
    for mismatch in [false, true] {
        let mut context = HarnessToolsContext::new(Boundary::Remote, Arc::new(Token));
        context.api_base_url = Some("https://harness.example".into());
        context.remote_transport = Some(Arc::new(Transport { mismatch }));
        let tools = build_catalog(context).unwrap();
        let args = json!({"job_id":"job-output","name":"exploratory_outputs.json"});
        let result = tools
            .resolve("read_artifact")
            .unwrap()
            .call(args.as_object().unwrap());
        if mismatch {
            assert!(result.unwrap_err().to_string().contains("attribution"));
        } else {
            assert_eq!(result.unwrap()["content"], "{\"programs\":{}}");
        }
    }
}
