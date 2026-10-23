use mcp::auth::bearer_headers;
use mcp::auth::BearerTokenSource;
use mcp::auth::MissingBearerCredentialError;
use mcp::tools::catalog::build_catalog;
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
    failure: bool,
}
impl RemoteTransport for Transport {
    fn get(&self, _: &str, _: &BTreeMap<String, String>) -> Result<RemoteResponse, String> {
        panic!("no GET")
    }
    fn post(
        &self,
        url: &str,
        headers: &BTreeMap<String, String>,
        payload: &Value,
    ) -> Result<RemoteResponse, String> {
        assert_eq!(url, "https://harness.example/mcp");
        assert_eq!(headers["Authorization"], "Bearer test-token");
        assert!(matches!(
            payload["params"]["name"].as_str(),
            Some("validate_strategy" | "validate_study")
        ));
        assert_eq!(payload["params"]["arguments"]["model"]["source"], "source");
        Ok(RemoteResponse {
            status_code: 200,
            headers: BTreeMap::new(),
            body: json!({
                "jsonrpc": "2.0", "id": 1, "result": {"isError": self.failure,
                    "content": [{"type": "text", "text": "compiler error"}],
                    "structuredContent": {"valid": !self.failure, "deterministicCompilerDiagnostic": self.failure,
                        "diagnostics": [{"message": "mismatched types"}]}}
            }),
        })
    }
}

#[test]
fn validation_catalog_shape_and_capabilities_match_in_both_modes() {
    for mode in [Boundary::Local, Boundary::Remote] {
        let mut context = HarnessToolsContext::new(mode, Arc::new(Token));
        context.api_base_url = Some("https://harness.example".into());
        context.remote_transport = Some(Arc::new(Transport { failure: false }));
        let catalog = build_catalog(context).unwrap();
        let strategy = catalog.resolve("validate_strategy").unwrap();
        assert_eq!(strategy.capability, "backtest");
        assert_eq!(
            strategy.input_schema,
            catalog.resolve("validate_study").unwrap().input_schema
        );
        assert!(strategy.description.contains("export_strategy_program!"));
        let mut context = HarnessToolsContext::new(mode, Arc::new(Token));
        context.api_base_url = Some("https://harness.example".into());
        context.remote_transport = Some(Arc::new(Transport { failure: false }));
        context.capabilities.backtest = false;
        let disabled = build_catalog(context).unwrap();
        assert!(disabled.resolve("validate_strategy").is_none());
        assert!(disabled.resolve("validate_study").is_some());
    }
}

#[test]
fn remote_validation_forwards_authenticated_model_and_preserves_diagnostics() {
    for failure in [false, true] {
        let mut context = HarnessToolsContext::new(Boundary::Remote, Arc::new(Token));
        context.api_base_url = Some("https://harness.example".into());
        context.remote_transport = Some(Arc::new(Transport { failure }));
        let catalog = build_catalog(context).unwrap();
        for name in ["validate_strategy", "validate_study"] {
            let result = catalog
                .resolve(name)
                .unwrap()
                .call(json!({"model": {"source": "source"}}).as_object().unwrap());
            if failure {
                let error = result.unwrap_err();
                assert_eq!(error.message(), "compiler error");
                assert_eq!(
                    error.payload().unwrap()["deterministicCompilerDiagnostic"],
                    true
                );
                assert_eq!(
                    error.payload().unwrap()["diagnostics"][0]["message"],
                    "mismatched types"
                );
            } else {
                assert_eq!(result.unwrap()["valid"], true);
            }
        }
    }
}
