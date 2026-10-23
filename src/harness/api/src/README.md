# Compose harness entrypoints

This folder composes native execution, configuration, persistence and protocol adapters. Engines belong to backtest and authored compilation to strategy-jit; this layer allocates no replacement job identities.

- `lib.rs`: Defines the Clap process facade and standalone job command seams, and exports HTTP and local composition modules.
- `local.rs`: Freezes authored input at admission and supplies worker-only native execution, progress and file publication adapters.
- `native.rs`: Runs standalone backtest, study and screening CLI jobs through the native engine and JIT with the configured lake client.
- `mcp_cli.rs`: Parses MCP CLI settings and launches the composed protocol server.

`execution/` owns durable queued admission, isolated finite workers and local/Azure
execution adapters. `http/` only accepts and observes work through injected ports.

- `bin/`: Local bin capability.
