use std::sync::Arc;

use mcp::auth::EnvironmentBearerTokenSource;
use mcp::tools::{build_catalog, Boundary, HarnessToolsContext, TOOL_VALIDATE_STUDY};
use serde_json::json;

#[test]
fn study_catalog_advertises_supported_configuration_and_current_sdk() {
    let context = HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    let catalog = build_catalog(context).unwrap();
    let tool = catalog.resolve(TOOL_VALIDATE_STUDY).unwrap();
    let configuration =
        &tool.input_schema["properties"]["model"]["properties"]["harnessConfiguration"];
    assert_eq!(configuration["required"], json!(["start", "end"]));
    assert_eq!(configuration["anyOf"].as_array().unwrap().len(), 3);
    let properties = &configuration["properties"];
    for key in ["start", "end"] {
        assert_eq!(properties[key]["format"], "date-time");
        assert_eq!(properties[key]["pattern"], "(Z|[+-][0-9]{2}:[0-9]{2})$");
        assert!(properties[key]["description"]
            .as_str()
            .unwrap()
            .contains("offset"));
    }
    assert_eq!(properties["interval"]["enum"], json!(["1min", "1d"]));
    let nested = &properties["data"]["properties"];
    assert_eq!(
        nested["bar_interval"]["enum"],
        properties["interval"]["enum"]
    );
    assert_eq!(nested["subscriptions"], properties["subscriptions"]);
    let branches = &properties["subscriptions"]["items"]["oneOf"];
    assert_eq!(
        branches[0]["required"],
        json!(["data_type", "symbols", "interval"])
    );
    assert_eq!(
        branches[0]["properties"]["interval"]["enum"],
        json!(["1min", "1d"])
    );
    assert_eq!(
        branches[1]["properties"]["data_type"]["enum"],
        json!(["quote", "trade"])
    );
    assert_eq!(branches[1]["additionalProperties"], false);
    assert!(branches[1]["properties"].get("interval").is_none());
    for key in ["symbols", "requiredSymbols", "required_symbols"] {
        assert_eq!(properties[key]["minItems"], 1);
    }
    for convention in [
        "type Context = ObservationContext",
        "fn subscriptions(&self) -> Vec<Subscription>",
        "fn on_bar(&mut self, frame: &BarFrame, ctx: &mut ObservationContext)",
        "bars_by_symbol",
        "StudyProgram::new(parameters: Value) -> Self",
        "metrics.publish",
        "export_study_program!(MyStudy)",
        "call validate_study again",
        "does not replay",
        "infrastructure failures",
    ] {
        assert!(
            tool.description.contains(convention),
            "missing {convention}"
        );
    }
    assert!(!tool.description.contains("verbatim"));
    for name in ["author_study", "update_study", "read_study"] {
        assert!(catalog.resolve(name).is_none());
    }
}

#[test]
fn study_capability_controls_validation_and_replay_together() {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.capabilities.study = false;
    let catalog = build_catalog(context).unwrap();
    assert!(catalog.resolve(TOOL_VALIDATE_STUDY).is_none());
    assert!(catalog.resolve("run_study").is_none());
}
