# Harness transport integration

This folder adapts harness transport, activation, and capability discovery for
Thesis. Runtime execution and chat presentation remain with their owners.

- `__init__.py`: exports the intentional integration facade.
- `capabilities.py`: typed capability status and availability reports.
- `harness_activation.py`: explicit harness activation requests and adapters.
- `mcp.py`: MCP transports, discovery including read-only annotations, protocol
  validation, and tool results with outcome certainty.
- `runtime_status.py`: derives harness runtime status for callers.
