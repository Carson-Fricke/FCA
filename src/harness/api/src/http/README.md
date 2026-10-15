# Serve harness HTTP requests

This folder owns authenticated HTTP composition and injected execution ports. The routes subfolder maps requests to these ports; native computation and durable job definitions remain in sibling crates.

- `mod.rs`: Exports HTTP composition modules.
- `auth.rs`: Authenticates configured bearer credentials.
- `cli.rs`: Resolves HTTP CLI settings, composes the complete local/Azure market-data capability under `/market-data` and injects native access and launches the combined server.
- `clock.rs`: Defines injectable timestamps.
- `configuration.rs`: Validates submitted configurations and immutable admission fingerprints.
- `launcher.rs`: Defines execution ports and composes admitted job start, progress persistence and terminal publication.
- `mcp.rs`: Mounts the MCP transport in HTTP composition.
- `models.rs`: Defines HTTP request and response models.
- `native.rs`: Adapts native JIT study/backtest execution to the HTTP runner port with job-scoped progress.
- `screener.rs`: Defines screening launch and result adapters.
- `server.rs`: Builds and serves the HTTP router.
- `state.rs`: Holds injected HTTP dependencies and settings.
- `store.rs`: Re-exports the shared MCP-owned durable job contract.
