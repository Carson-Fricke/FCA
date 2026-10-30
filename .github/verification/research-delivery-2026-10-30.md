# Research, SDK and conversation delivery — 2026-10-30

## Delivery scope

The user requested a local restart, then commit and push. Following `AGENTS.md`,
this delivery includes every pending non-ignored repository change, including
earlier SDK producers/consumers, runtime/UI work, numeric parameter handling and
Windows publication changes. Those earlier implementations are separate from the
bounded review-loop implementation documented in
`proportionate-research-review-2026-10-30.md`. Existing verification records were
retained without rewriting their results.

## Integration corrections

The complete delivery check found 27 UI type errors in pending transcript work.
SSE callbacks now expose the canonical `TimelineItemWire` they actually decode.
The display adapter projects tool results as display records; history test
fixtures use display receipts while live stream fixtures retain canonical
messages. Backend JSON fixtures are decoded through current schemas, retired
tree fields are removed from fixture expectations, and the scoped oldest-item
derivation uses a deferred callback. No backend delivery path changed.

An incremental wheel build contained a retired creation module left in the
ignored build cache. That cache was preserved under
`.local/reviews/stale-build-20261030-delivery`, and a clean wheel was rebuilt.
Inspection confirmed the retired creation and standalone SDK modules are absent.
No cache or build artifact is included in the commit.

## Verification

- Python source remained unchanged after the immediately preceding full
  integration run: **1817 passed, 1 opt-in live Cosmos test skipped**, in 129.98 s.
  Lockfile, repository Ruff lint/format and ty checks passed in that run. The
  mandatory delivery architecture and behavior suites were included.
- `cargo +1.98.1 fmt --manifest-path src/harness/Cargo.toml --all --check`: passed.
- `cargo +1.98.1 clippy --manifest-path src/harness/Cargo.toml --workspace --all-targets --locked -- -D warnings`: passed.
- `cargo +1.98.1 test --manifest-path src/harness/Cargo.toml --workspace --locked`:
  passed, including JIT compilation, SDK doctests and Windows replacement behavior.
- UI `npm run check`: no errors; three CSS compatibility warnings.
- UI `npm test`: **287 passed, 1 optional live test skipped**, across 26 files.
- UI `npm run build`: passed.
- `node --test scripts/ci/*.test.mjs`: **13 passed**.
- Bicep build and lint: passed with nullability/minimum-length warnings on
  unchanged expressions. Build output remained under ignored `.local/reviews`.
- Canonical isolated wheel build and installed SDK smoke: passed. The installed
  dependency graph, metadata, source references and templates agree outside the
  checkout. The clean wheel has no retired creation/standalone SDK modules.
- Staged credential/local-artifact scan: no high-confidence key patterns or
  local-only paths found. Repository LF and staged whitespace checks passed.

No live model research or browser acceptance run was initiated for this delivery.

## Local activation

Restarted Thesis with `scripts/local/start-stack.ps1 -Servers thesis -Foreground`
and UI with `-Servers ui -Foreground`. Restart logs:

- `.local/logs/restart-20261030-112830.log`
- `.local/logs/restart-20261030-114048.log`

Authenticated workflow inspection confirmed `research-investigation@6.0.0` and
all five adjudicate routes. Thesis and harness health endpoints and the UI HTTP
endpoint passed after the restarts. The harness implementation was unchanged
during these delivery corrections and remained running.
