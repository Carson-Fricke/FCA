# Compose harness entrypoints

This folder composes native execution, configuration, persistence and protocol adapters. Engines belong to backtest and authored compilation to strategy-jit; this layer allocates no replacement job identities.

- `lib.rs`: Defines the Clap process facade and standalone job command seams, and exports HTTP and local composition modules.
- `local.rs`: Builds local MCP tools, admits jobs, injects job progress persistence and publishes native execution outcomes.
- `native.rs`: Runs standalone backtest, study and screening CLI jobs through the native engine and JIT with the configured lake client.
- `mcp_cli.rs`: Parses MCP CLI settings and launches the composed protocol server.
