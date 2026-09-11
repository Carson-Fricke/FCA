use super::*;

fn temp_config(name: &str, body: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-api-cli-{}-{name}.yaml",
        std::process::id()
    ));
    std::fs::write(&path, body).expect("write");
    path
}

#[test]
fn loads_server_and_auth_fields() {
    let path = temp_config(
            "api",
            "shell: api\nserver:\n  host: 0.0.0.0\n  port: 9000\n  workers: 1\nauth:\n  bearer_token: \"${HARNESS_API_BEARER_TOKEN}\"\n",
        );
    let settings = load_api_settings(&path).expect("load");
    assert_eq!(settings.shell, "api");
    assert_eq!(settings.host, "0.0.0.0");
    assert_eq!(settings.port, 9000);
    assert_eq!(settings.workers, 1);
    assert_eq!(
        settings.bearer_reference.as_deref(),
        Some("${HARNESS_API_BEARER_TOKEN}")
    );
    let _ = std::fs::remove_file(path);
}

#[test]
fn missing_config_is_a_usage_error() {
    let error = resolve_api_settings(&crate::ApiArgs::default()).expect_err("error");
    assert!(error.0.contains("--config"));
}

#[test]
fn non_api_shell_is_rejected() {
    let path = temp_config("backtest", "shell: backtest\n");
    let args = crate::ApiArgs {
        config: Some(path.clone()),
        host: None,
        port: None,
    };
    let error = resolve_api_settings(&args).expect_err("error");
    assert!(error.0.contains("shell must be 'api'"));
    let _ = std::fs::remove_file(path);
}

#[test]
fn environment_reference_resolution_names_the_variable() {
    let error =
        resolve_environment_reference("${FCA_DEFINITELY_UNSET_VARIABLE}").expect_err("error");
    assert!(error.0.contains("FCA_DEFINITELY_UNSET_VARIABLE"));
    assert!(resolve_environment_reference("literal").expect("literal") == "literal");
}

#[test]
fn api_args_parse_from_cli() {
    let cli = ApiCli::try_parse_from([
        "api",
        "--config",
        "config/harness/api.yaml",
        "--host",
        "0.0.0.0",
        "--port",
        "9001",
    ])
    .expect("parse");
    assert_eq!(cli.args.host.as_deref(), Some("0.0.0.0"));
    assert_eq!(cli.args.port, Some(9001));
}
