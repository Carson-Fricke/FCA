use super::*;
use crate::auth::{bearer_headers, EnvironmentBearerTokenSource};

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

fn local_context() -> HarnessToolsContext {
    let mut context =
        HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
    context.runner = Some(Arc::new(EchoRunner));
    context.validate().expect("valid context")
}

#[test]
fn default_catalog_is_capability_filtered() {
    let catalog = build_catalog(local_context()).expect("catalog");
    assert_eq!(
        catalog.names(),
        vec![
            "run_backtest",
            "backtest_config_template",
            "get_backtest_status",
            "get_run",
            "list_artifacts",
            "read_artifact",
            "run_sweep"
        ]
    );
    let mut opted_in = local_context();
    opted_in.capabilities.trader = true;
    let catalog = build_catalog(opted_in).expect("catalog");
    assert!(catalog.names().contains(&"launch_trader"));
}

#[test]
fn run_backtest_invokes_the_local_runner() {
    let catalog = build_catalog(local_context()).expect("catalog");
    let tool = catalog.resolve(TOOL_RUN_BACKTEST).expect("tool");
    let mut arguments = Map::new();
    arguments.insert("config_path".to_string(), json!("backtest.yaml"));
    arguments.insert("workspace".to_string(), json!("out"));
    let payload = tool.call(&arguments).expect("call");
    assert_eq!(payload["boundary"], "local");
    assert_eq!(payload["exit_code"], 0);
    assert_eq!(
        payload["stdout"],
        "harness backtest-job --config backtest.yaml --output-dir out"
    );
}

#[test]
fn settings_never_contain_a_token() {
    let context = local_context();
    let settings = context.as_settings();
    assert_eq!(settings["bearer_env_var"], BEARER_TOKEN_VARIABLE);
    assert!(!settings.to_string().contains("Bearer "));
    let _ = bearer_headers(Some("x"));
}

#[test]
fn remote_boundary_requires_https_and_transport() {
    let mut context =
        HarnessToolsContext::new(Boundary::Remote, Arc::new(EnvironmentBearerTokenSource));
    context.api_base_url = Some("http://example.test".to_string());
    assert!(context.validate().is_err());
}
