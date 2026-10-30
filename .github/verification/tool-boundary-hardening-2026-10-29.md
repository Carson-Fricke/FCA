# Tool boundary hardening verification

Implemented the approved input guidance, publication concurrency, error delivery,
and SDK reference fixes. Completion wake scheduling and batch semantics were not
changed.

- Queued-job receipts and shared child guidance distinguish automatic completion
  delivery from operator-input pauses.
- Existing process-safe workspace publication locks wait for up to 30 seconds,
  checking ownership during acquisition and before entering the transaction.
  Lock timeout reports a known not-started publication. Independent program
  publications and existing content-version checks are verified.
- Failed tool executions require a nonempty category and explanation.
  Structured domain failures retain diagnostics, available selectors, recovery
  arguments and guidance through adapter catches, canonical delivery, HTTP
  translation and provider projection. Native MCP errors always retain a
  structured payload. Unexpected exceptions still use conservative failure
  classification; the platform cannot invent domain constraints omitted by an
  operation's implementation.
- Missing output selectors are derived from the actual artifact. Pending output
  reads distinguish not-ready jobs from unavailable terminal artifacts.
- SDK aliases have one SDK-owned manifest. Python and native readers accept
  automatically discovered source paths without per-element registration. The
  manifest ships in wheels/source distributions and participates in SDK identity.

Verification:

- Full Python suite: **1808 passed, 1 skipped in 110.98 seconds**. Includes the
  mandatory delivery architecture, interruption, acknowledgement and admission
  behavior suites. The optional live Cosmos latency test remains skipped.
- The first integration run exposed obsolete test failure fixtures and an SDK
  checkout fixture missing the new manifest; these were corrected. Read-only
  failure certainty remains observed failure where the read reached its target.
- SDK reference integration tests: 3 passed, including all discovered source
  paths and agreement between native/Python source inventory identities.
- MCP crate tests: 18 passed. Clippy passed for MCP and strategy JIT, all targets.
- Repository Ruff lint/format, ty, diff whitespace and LF checks passed.
- Built-wheel SDK smoke passed with repository modules excluded from imports;
  exercised the alias manifest, source inventory, templates and source-path reads.
- Rebuilt/restarted local harness and Thesis. Both health endpoints passed;
  the live serving SDK identity matched local sources and a discovered-path
  reference read succeeded.

The new failure contract updates current producers and fixtures rather than
accepting failed outcomes without explanations. No alternate inbox, diagnostic
queue, provider-role mapping or execution replay path was added.
