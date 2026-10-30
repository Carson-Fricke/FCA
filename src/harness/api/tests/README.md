# Harness boundary behavior

External providers and stores use isolated fakes. The coordinator owns full
workspace verification. These tests retain job identity, interruption and
acknowledgement coverage required by the execution contract.

- `configuration_contract.rs`: strict submitted configuration and retained identity.
- `entrypoints.rs`: public CLI routing and required process entrypoints.
- `execution_pool.rs`: durable duplicate/conflicting admission, lost notifications, expired-owner fencing, bounded startup reservations, streamed artifacts and responsive health during blocking control work. Replaces the former thread-specific worker check in `job_id_receipt.rs`.
- `http_parity.rs`: authenticated HTTP launch/query/result behavior with injected runners.
- `job_id_receipt.rs`: native backtest, study and screening receipts retain the caller's ID and override document placeholders.
- `job_completions.rs`: Real worker completion POST delivery, durable failure retry, immutable terminal evidence and acknowledgement behavior without execution replay.
- `job_metrics.rs`: complete native result metrics survive publication.
- `mcp_http.rs`: authenticated MCP catalog, shared admission and result access.
- `sdk_metadata.rs`: SDK reference and validation provenance.
- `strategy_validation.rs`: real authored strategy compilation, loading, configuration diagnostics and isolated HTTP validation.
- `study_validation.rs`: study validation and authored SDK contracts.
- `fixtures/`: authored programs used at these public boundaries.
- `unit/`: pure Azure protocol identity parsing; ownership, interruption and publication remain public-boundary behavior tests.
