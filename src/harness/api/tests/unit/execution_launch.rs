//! Pure Azure protocol identity validation; execution ownership is covered at HTTP.
use super::acknowledged_execution;
use super::start_template;
use serde_json::json;

#[test]
fn job_start_uses_only_the_execution_wire_schema() {
    let container = json!({"name":"worker","image":"repo@sha256:abc","command":["sh"],
        "args":["exec harness-worker"],"env":[],"resources":{"cpu":4,"memory":"8Gi"}});
    let mut definition = json!({"containers":[container],"volumes":[],"initContainers":null});
    definition["containers"][0]["probes"] = json!([]);
    definition["containers"][0]["volumeMounts"] = json!(null);
    assert_eq!(
        start_template(&definition).unwrap(),
        json!({"containers":[container]})
    );
    definition["volumes"] = json!([{"name":"scratch"}]);
    assert!(start_template(&definition).is_err());
}

#[test]
fn minimal_azure_acknowledgements_resolve_only_one_scoped_execution() {
    let resource = "/subscriptions/s/resourceGroups/g/providers/Microsoft.App/jobs/j";
    let expected = format!("{resource}/executions/j-123");
    for value in [
        json!({"name":"j-123"}),
        json!({"id":expected}),
        json!({"name":"j-123","id":expected}),
    ] {
        assert_eq!(acknowledged_execution(resource, &value).unwrap(), expected);
    }
    for value in [
        json!({}),
        json!({"name":"../j"}),
        json!({"id":"/other/executions/j-123"}),
        json!({"name":"j-456","id":expected}),
        json!({"id":format!("{expected}/other")}),
    ] {
        assert!(acknowledged_execution(resource, &value).is_err());
    }
}
