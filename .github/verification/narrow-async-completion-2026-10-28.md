# Narrow asynchronous completion — 2026-10-28

Removed the generic background tool executor, registration flags, deferred dispatch
hooks and worker recovery path. Result normalization is back in the existing
session loop; MCP exchange locking is restored. The superseded background
validation acceptance suite is deleted, and study recovery uses ordinary dispatch.
Mandatory delivery guards still verify direct canonical inbox delivery.

Retained immediate job admission, concise no-poll prompting, retained submission
identity, terminal job notifications and transcript-derived pending returns.
Previously agreed context, SDK, diagnostics and strategy fixes remain intact.
The local PostgreSQL audit found no background-tool work records; no data changed.

Verification: 128 focused tests passed. Repository Ruff lint/format, ty, lockfile,
LF and diff checks passed. The first full run passed in 187.15 seconds while
sharing resources with lint/type checks, exceeding the time budget. The permitted
final run executed alone: **1,792 passed, 1 skipped, 179.62 seconds**. No test
coverage was removed to meet timing. Evidence is in
`.local/reviews/narrow-async-python.log` and `narrow-async-python-final.log`.

Restarted Thesis through the canonical local launcher (new listener PID 12716).
Thesis and Harness health checks returned `ok`; completion intake returned 401
without credentials and 204 for an authenticated untracked event. The smoke event
created no job or conversation. No native source changed in this narrowing step.
Restart log: `.local/logs/restart-20261028-220302.log`. No commit or push.
