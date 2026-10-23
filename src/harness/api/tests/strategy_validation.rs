use api::local::authored_strategy_document;
use api::local::NativeStudyValidator;
use axum::body::Body;
use axum::http::Request;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use mcp::tools::ports::StudyValidator;
use serde_json::json;
use serde_json::Value;
use tower::ServiceExt;

const SOURCE: &str = r#"
use harness_strategy_sdk::*;
struct Dependency { sequence: IndicatorSequence }
impl MarketConsumer for Dependency {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Trade { symbols: vec!["SPY".into()] }]
    }
}
impl Indicator for Dependency { fn value(&self, _: &str) -> Option<Decimal> { None }
fn sequence(&self) -> &IndicatorSequence { &self.sequence }
fn sequence_mut(&mut self) -> &mut IndicatorSequence { &mut self.sequence } }
struct Strategy { dependency: Dependency }
impl MarketConsumer for Strategy {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1d".into() }]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> { vec![&mut self.dependency] }
}
impl StrategyProgram for Strategy {
    fn new(_: serde_json::Value) -> Self { Self { dependency: Dependency { sequence: IndicatorSequence::ticks(60_000_000_000).unwrap() } } }
}
export_strategy_program!(Strategy);
"#;

fn model(source: &str) -> Value {
    json!({"source": source, "harnessConfiguration": {
        "start": "2025-01-01T00:00:00-05:00", "end": "2025-01-02T00:00:00-05:00",
        "subscriptions": [
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1d"},
            {"data_type": "trade", "symbols": ["SPY"]}
        ]
    }})
}

#[test]
fn native_strategy_validation_really_compiles_loads_constructs_and_checks_dependencies() {
    let input = model(SOURCE);
    let document = authored_strategy_document(&input).unwrap();
    assert_eq!(document["shell"], "backtest");
    let result = NativeStudyValidator
        .validate_strategy_result(&input)
        .unwrap();
    assert_eq!(result["valid"], true);
    assert_eq!(result["kind"], "strategy");
    assert_eq!(result["shell"], "backtest");
    assert_eq!(result["subscriptions"].as_array().unwrap().len(), 2);
    assert_eq!(
        NativeStudyValidator
            .validate_strategy_result(&input)
            .unwrap()["reused"],
        true
    );

    let mut mismatch = input.clone();
    mismatch["harnessConfiguration"]["subscriptions"] = json!([
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1d"}
    ]);
    let error = NativeStudyValidator
        .validate_strategy_result(&mismatch)
        .unwrap_err();
    assert!(error.message().contains("subscription union"));
    assert_eq!(
        error.payload().unwrap()["deterministicCompilerDiagnostic"],
        false
    );

    let failure = NativeStudyValidator
        .validate_strategy_result(&model("fn broken() { let _: u32 = \"wrong\"; }"))
        .unwrap_err();
    assert_eq!(
        failure.payload().unwrap()["deterministicCompilerDiagnostic"],
        true
    );
    assert!(!failure.payload().unwrap()["diagnostics"]
        .as_array()
        .unwrap()
        .is_empty());
    assert!(failure.message().contains("validate_strategy again"));
    assert!(!failure
        .payload()
        .unwrap()
        .to_string()
        .contains("explanation"));

    let missing = NativeStudyValidator
        .validate_strategy_result(&model("pub fn no_exports() {}"))
        .unwrap_err();
    assert!(missing
        .message()
        .contains("export_strategy_program!(MyStrategy)"));
    assert!(missing.message().contains("OrderSink"));
    assert!(!missing.message().contains("export_study_program"));

    let panic_source = SOURCE.replace(
        "Self { dependency: Dependency { sequence: IndicatorSequence::ticks(60_000_000_000).unwrap() } }",
        "panic!(\"construction rejected\")",
    );
    let rejected = NativeStudyValidator
        .validate_strategy_result(&model(&panic_source))
        .unwrap_err();
    assert!(rejected.message().contains("creation failed"));
    assert_eq!(
        rejected.payload().unwrap()["deterministicCompilerDiagnostic"],
        false
    );

    // Identical input is not a valid study: kind is part of the validation cache key.
    assert!(NativeStudyValidator.validate_result(&input).is_err());
}

#[tokio::test]
async fn authenticated_http_mcp_exposes_and_executes_native_strategy_validation() {
    let mut config = api::http::state::AppConfig::in_memory();
    config.bearer_token = Some("test-token".into());
    config.mcp_server = Some(
        api::http::mcp::compose_with_validator(
            config.job_store.clone(),
            std::sync::Arc::new(api::execution::dispatch::ConfiguredWake),
            std::sync::Arc::new(api::execution::validation::ProcessValidator {
                binary: std::path::PathBuf::from(env!("CARGO_BIN_EXE_harness-worker")),
            }),
        )
        .unwrap(),
    );
    let app = api::http::create_app(config).unwrap();
    let input = model(&format!("{SOURCE}\n// isolated HTTP transport"));
    let request = json!({"name": "validate_strategy", "arguments": {"model": input}});
    let exits = SOURCE.replace(
        "Self { dependency: Dependency { sequence: IndicatorSequence::ticks(60_000_000_000).unwrap() } }",
        "std::process::exit(91)",
    );
    for (index, (method, params)) in [
        ("tools/list", json!({})),
        ("tools/call", request.clone()),
        ("tools/call", request),
        (
            "tools/call",
            json!({"name":"validate_strategy","arguments":{"model":model(&exits)}}),
        ),
    ]
    .into_iter()
    .enumerate()
    {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp")
                    .header("Authorization", "Bearer test-token")
                    .header("Content-Type", "application/json")
                    .body(Body::from(
                        json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body: Value =
            serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes())
                .unwrap();
        if method == "tools/list" {
            assert!(body["result"]["tools"]
                .as_array()
                .unwrap()
                .iter()
                .any(|tool| tool["name"] == "validate_strategy"));
        } else if index == 3 {
            assert_eq!(body["result"]["isError"], true, "{body}");
            assert_eq!(body["result"]["structuredContent"]["cacheable"], false);
        } else {
            assert_eq!(body["result"]["isError"], false, "{body}");
            assert_eq!(body["result"]["structuredContent"]["kind"], "strategy");
            assert_eq!(body["result"]["structuredContent"]["shell"], "backtest");
            assert_eq!(body["result"]["structuredContent"]["reused"], index == 2);
        }
    }
    let healthy = app
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(healthy.status(), StatusCode::OK);
}

#[test]
fn authored_strategy_configuration_requires_explicit_subscriptions() {
    let input = json!({"source": SOURCE, "harnessConfiguration": {
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z",
        "symbols": ["SPY"],
        "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1d"}]
    }});
    let document = authored_strategy_document(&input).unwrap();
    assert_eq!(
        document["data"]["subscriptions"],
        json!([
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1d"}
        ])
    );
    let interval_only = json!({"source": SOURCE, "harnessConfiguration": {
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z",
        "interval": "1d", "symbols": ["SPY"]
    }});
    assert!(authored_strategy_document(&interval_only)
        .unwrap_err()
        .contains("interval"));
}
