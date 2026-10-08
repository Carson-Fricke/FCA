use super::*;
use crate::auth::EnvironmentBearerTokenSource;
use crate::store::InMemoryJobStore;
use crate::tools::{Boundary, ToolExecutionError};

struct Validator(Option<String>);

impl StudyValidator for Validator {
    fn validation_identity(&self) -> Option<String> {
        self.0.clone()
    }
    fn validate(&self, _: &Value) -> Result<Value, String> {
        unreachable!()
    }
    fn validate_result(&self, _: &Value) -> Result<Value, ToolExecutionError> {
        Err(ToolExecutionError::with_payload(
            "authored error",
            json!({
                "deterministicCompilerDiagnostic": true, "reused": true,
                "diagnostics": [{"code": {"code": "E0308"}}],
            }),
        ))
    }
}

fn server(validator: Option<Arc<dyn StudyValidator>>) -> McpServer {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.job_store = Some(Arc::new(InMemoryJobStore::new()));
    context.study_validator = validator;
    create_server(context).unwrap()
}

#[test]
fn initialize_only_serializes_injected_opaque_identity() {
    for identity in [None, Some("remote-validator:opaque".to_string())] {
        let server = server(Some(Arc::new(Validator(identity.clone()))));
        let result = server
            .handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
            .unwrap();
        assert_eq!(
            result["result"]["serverInfo"]["validationIdentity"].as_str(),
            identity.as_deref()
        );
    }
    let result = server(None)
        .handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        .unwrap();
    assert!(result["result"]["serverInfo"]
        .get("validationIdentity")
        .is_none());
}

#[test]
fn validator_diagnostics_and_reuse_pass_through_without_inference() {
    let server = server(Some(Arc::new(Validator(None))));
    let response = server
        .handle(&json!({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "validate_study", "arguments": {"model": {}}},
        }))
        .unwrap();
    assert_eq!(response["result"]["isError"], true);
    assert_eq!(response["result"]["structuredContent"]["reused"], true);
    assert_eq!(
        response["result"]["structuredContent"]["deterministicCompilerDiagnostic"],
        true
    );
    assert_eq!(
        response["result"]["structuredContent"]["diagnostics"][0]["code"]["code"],
        "E0308"
    );
}
