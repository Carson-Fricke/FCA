use api::local::NativeStudyValidator;
use mcp::tools::StudyValidator;
use serde_json::{json, Map};

#[test]
fn native_serving_sdk_metadata_and_initialize_match_source_reference_identity() {
    let metadata = NativeStudyValidator.sdk_reference(&Map::new()).unwrap();
    let sdk = NativeStudyValidator
        .sdk_reference(
            json!({"topic": "consumer", "start_line": 1, "end_line": 5})
                .as_object()
                .unwrap(),
        )
        .unwrap();
    assert_eq!(sdk["sdkIdentity"], metadata["sdkIdentity"]);
    assert_eq!(metadata["sdkIdentity"].as_str().unwrap().len(), 64);
    assert!(sdk["content"].as_str().unwrap().contains("subscription"));
    let server = api::http::mcp::compose_local_server(std::sync::Arc::new(
        mcp::store::InMemoryJobStore::new(),
    ))
    .unwrap();
    let response = server
        .handle(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        .unwrap();
    for key in [
        "sdkIdentity",
        "sdkVersion",
        "sdkAbiVersion",
        "validationIdentity",
    ] {
        assert_eq!(response["result"]["serverInfo"][key], metadata[key]);
    }
}
