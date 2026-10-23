# thesis_runtime/hosting

Generic worker composition and recovery through version-pinned workflow registrations. Application startup injects the configured runtime host and storage ports.

- `__init__.py`: Transport-independent runtime composition and worker hosting.
- `worker.py`: Bind generic durable worker execution and version-pinned workflow recovery. Reconcile committed complete outputs as success and sealed steering partials as interruption, without replay. Isolate execution-specific recovery faults and retain distinct diagnostics as phase activity without completing phases or replaying interrupted work.

Recovery traverses paged identity metadata, inspecting at most eight executions
per pass and hydrating launch/phase artifacts only for selected live executions.
The generic worker continues dispatch while a recovery pass waits on storage or
an extension. Batches continue promptly; completed sweeps back off for a minute.
