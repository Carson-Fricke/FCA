# tools

Local ownership and neighbors follow `.github/architecture/repository.md`.

- `arguments.rs`: Owns MCP argument validation and configuration materialization before dispatch.

- `catalog.rs`: Owns MCP discovery schemas and tool registration; delegates execution to handlers.

- `launch.rs`: Owns MCP launch behavior over the injected capabilities; no host composition.

- `mod.rs`: Owns MCP handler composition. Protocol ports, discovery and each operation family have separate owners.

- `ports.rs`: Owns MCP consumer ports and context construction; adapters supply implementations.

- `queries.rs`: Owns MCP queries behavior over the injected capabilities; no host composition.

- `templates.rs`: Owns MCP templates behavior over the injected capabilities; no host composition.

- `transport.rs`: Owns MCP transport behavior over the injected capabilities; no host composition.

- `validation.rs`: Owns MCP validation behavior over the injected capabilities; no host composition.
