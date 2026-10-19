---
applyTo: "src/lake/local/**,src/lake/cli/**,src/harness/market-reader/**,src/lake/ingest-worker/**,docs/local-market-lake.md,.env.example"
---

# Harness Market-Data Deployment Instructions

Run the complete harness in the selected environment. `HARNESS_MARKET_DATA_MODE=local` composes local filesystem catalog/objects/jobs and finite local worker processes. Azure composes storage adapters and finite ACA workers. Never make the default local harness depend on a remote lake service. Local Thesis/UI can instead select the entire remote harness.

Admission, immutable job identity, ownership, reconciliation, transactions and reading use shared application contracts. Native execution receives catalog/acquisition and reader ports directly. HTTP exists for separate clients/processes. Never add HTTP self-calls, inline API-owned provider work, local/remote algorithm copies, or legacy format fallbacks.

The main Azure API uses 1 vCPU / 2 GiB; ACA ingestion defaults to 4 vCPU / 8 GiB. Keep allocations configurable. The workstation launcher builds optimized binaries and configures local workers; `lake local reset --yes` remains explicit marker-protected cleanup.

Long ingestion plans are partitioned into disjoint date ranges under one retained job ID. Each finite ACA Job execution or local child owns an executor anchor and may claim further unstarted compatible chunks while retaining that anchor. Count physical executors, including cold boots, against the fleet limit. Preserve unknown-start reconciliation and never replay ACA start POSTs after a possible dispatch. All workers in an environment share the hard 10,000-request/minute ledger, including retries. Local and Azure currently must not ingest simultaneously with the same Alpaca allowance; keep local independent of remote coordination.

Provider credentials belong to the worker: managed identity/Key Vault in Azure, explicit environment or ignored provider file locally. Never print, persist in job documents, hard-code or commit credentials, bearer tokens or signed object URLs. API and object transports must not forward credentials to one another.

Keep manual real-data benchmarks to one closed symbol-session in isolated owned storage. Track accepted job IDs through completion, distinguish startup/dispatch from ingestion, compare row counts/checksums, and measure cold and warm reads. Covered reads do not prove ingestion. Automated tests use fake external services and never contact Alpaca or Azure.

Retain `src/lake/ingest-worker/tests/local.rs`, ingestion interruption/acknowledgement/duplicate-admission behavior and snapshot integrity tests. Documentation is not proof of compliance. Follow the current-contract and testing instructions in root `AGENTS.md`.
