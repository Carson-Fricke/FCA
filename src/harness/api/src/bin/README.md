# Harness process entrypoints

Entrypoints delegate to the harness application and do not own provider, storage, or engine policy.

- `harness.rs`: starts the unified harness CLI.
- `api.rs`: serves the combined harness HTTP/MCP host.
- `backtest_job.rs`: runs one admitted native backtest.
- `screen_job.rs`: runs one screening execution.
- `mcp.rs`: serves the standalone MCP transport.
- `market_data_profile.rs`: benchmarks one closed symbol-session through native or remote acquisition and verified cold/warm reads; outputs metadata and timings, never credentials or signed URLs.
