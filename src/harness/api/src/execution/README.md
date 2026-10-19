# Durable, isolated harness execution

This composition layer connects job admission to finite local processes or ACA
Jobs. Simulation remains in `backtest`, compilation in `strategy-jit`, and
market acquisition in `lake/hosted`. The API never executes authored code.

- `mod.rs`: selects one current local/Azure store, composes API ports and runs recovery notifications.
- `backend.rs`: implements atomic filesystem metadata and leased Azure Blob metadata, plus bounded artifact transfers.
- `store.rs`: retains immutable accepted input, conditional claims, renewable ownership, interrupted recovery and artifact inventories under `execution/`.
- `dispatch.rs`: adapts admitted submissions to coalesced capacity notifications; notification loss leaves work queued.
- `launch.rs`: reserves global startup capacity before launching a finite process or a digest-matched ACA Job. Unknown starts retain their reservation and are never retried.
- `worker.rs`: consumes startup reservations once, claims queued work, renews ownership, publishes results and exits. Dispatch and smoke modes only observe/notify durable work.
- `validation.rs`: runs compiler/loading/constructor validation in a bounded child with a deadline. The existing provenance-keyed validation service retains complete result caching in the parent.

One caller-allocated `job_id` survives admission, worker ownership and results.
REST and MCP admission use the same resolved configuration digest and frozen
input, so changing transport does not create a false conflict for identical work.
File-based CLI/MCP composition resolves and embeds source before admission;
HTTP accepts inline source only and rejects process-local source paths.
Running work whose owner expires becomes interrupted; recovery never queues it
again. Immutable artifact objects precede the fenced terminal metadata write.
API replicas can restart or scale independently of execution.

`HARNESS_EXECUTION_MAX_WORKERS` defaults to two. Startup reservations expire
after ten minutes; attached workers and execution ownership renew every ten
seconds with a sixty-second expiry. Azure uses the configured
`HARNESS_EXECUTION_JOB_RESOURCE_ID` and digest-pinned `HARNESS_EXECUTION_IMAGE`.
Local processes use the sibling `harness-worker` binary or the explicit
`HARNESS_WORKER_EXECUTABLE`. The worker and API must share the results backend.
Validation is serialized, limited to 1 GiB and 180 seconds, with an 8 MiB
response bound. Control requests and artifact transfers have separate bounds.
Inline artifact reads are limited to 64 MiB; raw downloads stream larger files
with four concurrent transfers and two 64 KiB queued buffers per transfer.
