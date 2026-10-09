# Inject harness tools into MCP

This folder owns MCP protocol serialization and injected harness tool ports.
It does not resolve Rust toolchains, inspect SDK sources, or cache validation
results. The injected `StudyValidator` supplies study and strategy compilation,
opaque provenance, structured diagnostics, and complete-result reuse metadata.

- `lib.rs` exposes the MCP modules.
- `auth.rs` supplies bearer credentials and authentication settings.
- `server.rs` dispatches JSON-RPC requests and serializes tool outcomes and
  optional validator-supplied `serverInfo.validationIdentity`.
- `tools.rs` declares injected ports, composes the tool catalog, and dispatches
  tool arguments to local or remote boundaries. `validate_study` and
  `validate_strategy` take authored model documents; compilation comes from the
  injected validator, with no structural-only strategy fallback.
- `store.rs` defines the authoritative durable job store: one `JobRecord` per
  `job_id` (`jobs/<job_id>.json`), artifact payloads under the `jobs/<job_id>/`
  object prefix, and adapters. Failed and cancelled jobs publish terminal
  records with an empty artifact set and their truthful `error_message`; a
  successful result still requires at least one artifact.
  `mark_running` persists worker start while preserving accepted input and
  rejecting terminal jobs; local MCP workers call it before doing any work.
- `transport.rs` serves the composed protocol through stdio and HTTP transports.

Focused protocol tests live in the sibling `tests/` folder. The API crate
composes local adapters; compiler and validation caches belong to strategy JIT.
Strategy validation and validator-identity fixtures live in
`tests/strategy_validation.rs` and `tests/unit/validation_identity.rs`.

Thesis's workspace wrapper exposes different, ID-only agent schemas: it resolves
`program_id` to latest committed `program.rs`/`manifest.json` before calling these
harness tools. MCP does not own workspace Git paths, publication, stored completion
references, or agent repair budgets. Compact diagnostics and minimal missing-export
templates are validator-owned guidance, not a protocol-side repair executor.
