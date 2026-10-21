# Durable, isolated harness execution

This composition layer connects job admission to finite local processes or ACA
Jobs. Simulation remains in `backtest`, compilation in `strategy-jit`, and
market acquisition in `lake/hosted`. The API never executes authored code.

- `mod.rs`: selects one current local/Azure store, composes API ports and runs recovery notifications.
- `backend.rs`: implements atomic filesystem metadata and ETag-conditional Azure metadata, plus bounded artifact transfers. Concurrent writers re-evaluate mutations against the latest version; no metadata blob lease blocks a heartbeat.
- `store.rs`: retains immutable accepted input, conditional claims, renewable ownership, interrupted recovery and artifact inventories under `execution/`. The ownership credential is private to the store; only sibling execution modules can access its backend. Callers obtain owned stores through `claim`, never by mutating owner fields.
- `dispatch.rs`: adapts admitted submissions to coalesced capacity notifications; notification loss leaves work queued.
- `launch.rs`: reserves global startup capacity before launching a finite process or a digest-matched ACA Job. Job definitions are projected to Azure's execution-only start schema. Minimal Azure name/ID acknowledgements are scoped and verified through an exact execution GET. Unknown starts retain their reservation and are never retried.
- `worker.rs`: consumes startup reservations once, claims queued work, renews ownership, publishes results and exits. Dispatch and smoke modes only observe/notify durable work.
- `renewal.rs`: retains acknowledged fleet/job deadlines, retries failed renewals, and reports structured storage errors. The renewal thread is separate from the process watchdog, including during host initialization, replay and publication.
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

Metadata version reads and conditional writes each have a five-second SDK
deadline. CAS conflicts may retry for five seconds; callbacks reset attempt-local
outputs before re-evaluation. Reservation, attachment and claim callbacks reconcile
their own operation tokens if the SDK retried an already-committed write and saw a
version conflict; this cannot allocate duplicate capacity or strand an owned job.
Other write failures retain unknown-outcome semantics.
Storage contention, transient failures, permission errors and permanent failures
are distinct from confirmed execution ownership loss. Failed renewals retry after
one second without extending acknowledged deadlines. An independent watchdog checks
those deadlines at most every 250 ms and stops the process on confirmed token loss
or expiry, even if the renewal SDK call is blocked. Logs retain the operation,
storage classification, resource, job and deadlines, and record recovery.

File publication reports its own stage and committed upload-byte counters. The
terminal commit preserves current progress and marks successful stages complete;
metrics and the artifact inventory become visible atomically after upload.
