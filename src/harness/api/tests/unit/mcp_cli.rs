use super::*;

#[test]
fn local_mode_composes_without_a_transport() {
    let args = crate::McpArgs {
        mode: "local".to_string(),
        api_base_url: None,
        enable_trader: false,
    };
    let server = build_mcp_server(&args).expect("server");
    assert!(server.catalog.resolve("run_backtest").is_some());
    assert!(server.catalog.resolve("launch_trader").is_none());
}

#[test]
fn enable_trader_publishes_the_tool() {
    let args = crate::McpArgs {
        mode: "local".to_string(),
        api_base_url: None,
        enable_trader: true,
    };
    let server = build_mcp_server(&args).expect("server");
    assert!(server.catalog.resolve("launch_trader").is_some());
}

#[test]
fn unknown_mode_is_rejected() {
    let args = crate::McpArgs {
        mode: "sideways".to_string(),
        api_base_url: None,
        enable_trader: false,
    };
    let error = build_mcp_server(&args).err().expect("error");
    assert!(error.0.contains("'local' or 'remote'"));
}

#[test]
fn remote_mode_requires_a_base_url() {
    let args = crate::McpArgs {
        mode: "remote".to_string(),
        api_base_url: None,
        enable_trader: false,
    };
    // Ensure the environment does not supply a base URL for this check.
    std::env::remove_var(API_BASE_URL_VARIABLE);
    assert!(build_mcp_server(&args).is_err());
}

#[test]
fn remote_mode_with_base_url_composes() {
    let args = crate::McpArgs {
        mode: "remote".to_string(),
        api_base_url: Some("https://api.example.test".to_string()),
        enable_trader: false,
    };
    let server = build_mcp_server(&args).expect("server");
    assert!(server.catalog.resolve("get_run").is_some());
}
