# Consume the lake HTTP contract

This folder is the harness lake boundary. It uses authenticated lake HTTP routes; provider credentials, ingestion policy, local lake paths and job identity allocation belong elsewhere.

- `lib.rs`: Exports the async client, blocking facade, request specifications and errors.
- `blocking.rs`: Bridges synchronous harness callers to the async transport, including incremental batch consumers and injected telemetry.
- `client.rs`: Implements coverage/ensure polling, reference queries and incremental Arrow IPC decoding with bounded inactivity and observed counters.
- `ensure.rs`: Classifies coverage and ensure-job outcomes and computes polling intervals.
- `error.rs`: Defines typed transport, protocol, coverage and decoding failures.
- `models.rs`: Deserializes lake coverage, ensure, job and reference response shapes.
- `spec.rs`: Builds ensure and read route arguments from dataset windows.
- `testing.rs`: Supplies fake lake HTTP responses and Arrow fixtures for consumer behavior tests.

Individual-option resolution uses POST /v1/options/resolve and validates the response identity, quote window, expiry, and delta tolerance before data acquisition. Async and blocking clients expose the same selector contract.

The default endpoint is the Azure development lake. `LAKE_API_URL`, then
`LAKE_API_BASE_URL`, override it; use `http://127.0.0.1:8002` for the local lake.
The caller supplies `LAKE_API__BEARER_TOKEN` for the selected endpoint. Credential
retrieval belongs to the process launcher, not this HTTP client.
