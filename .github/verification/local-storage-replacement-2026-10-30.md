# Local storage replacement verification

## Scope

The local execution-storage adapter now publishes new objects without clobbering
and uses Windows `ReplaceFileW` to replace existing objects. Non-Windows updates
retain tempfile's rename implementation. Both byte writes and artifact copies use
one publication helper; writer locks, staged-file synchronization, immutable
artifact admission, job identity, fencing and terminal-result rules remain owned
by their existing boundaries. No new public interface or workflow policy was added.

Publication errors include the destination, operation and OS error. Ordinary
failed replacements clean up staging files. Windows errors 1176/1177 can leave
the destination moved or absent, so these uncertain outcomes retain the staged
bytes and identify their path instead of deleting evidence or retrying. This
follows the documented failure states in
[Microsoft's ReplaceFileW contract](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew).

## Verification

- Real local-backend Windows regression: seven replacements with older readers
  retained throughout. Each retained reader sees its original complete bytes;
  fresh reads see the complete latest bytes. No staging files remain afterward.
- No-clobber creation: existing objects and published artifacts keep their bytes.
- Explicitly blocked Windows replacement: destination and OS error are reported,
  original bytes remain intact, and staging is cleaned up.
- Execution and execution-storage suites: **17 passed**, including restart,
  terminal publication, completion outbox acknowledgement, ownership, immutable
  input and duplicate-admission coverage.
- Storage Clippy with all targets and warnings denied passed. Workspace Rust
  formatting passed. Repository Ruff lint/format, ty, uv lock, whitespace and LF
  checks passed.
- Full Python integration suite: **1809 passed, 1 skipped in 130.58 seconds**,
  including the mandatory delivery architecture and behavior suites. The skipped
  test requires the explicitly enabled real Cosmos latency environment.

The regression tests run on this Windows host. Non-Windows code was not executed
here. Power-loss behavior and the rare Windows 1176/1177 failures were not fault
injected. The original historical failures lacked destination context; this fixes
the reproduced open-reader failure mechanism without claiming proof of the
specific reader responsible for each historical failure.

## Deployment

The local launcher rebuilds the optimized harness API and finite worker binaries
and restarts the harness listener. No harness worker was active before restart.
Thesis and the UI do not load this Rust storage adapter into their processes.

Completed: release harness API/worker build passed, harness listener restarted
from PID 4584 to PID 38068, and harness/Thesis health checks passed. Serving SDK
identity matches local sources and a discovered SDK reference read succeeded.
Thesis (PID 27552) and UI (PID 15972) remained running. Restart transcript:
`.local/logs/restart-20261030-110633.log` (ignored local evidence).
