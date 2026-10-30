# Agent research tooling iteration — 2026-10-28

## Implemented scope

- Shortened phase-specific instructions and removed repeated approval protocols,
  embedded SDK manuals and schema narration. Discovery's static assignment is
  517 words, down from approximately 2,657. Critics receive review duties rather
  than candidate-development instructions. Existing operator-acceptance behavior
  and the complete window-gated tool catalog remain intact.
- Workflow extensions select original context by explicit canonical message ID.
  The generic host resolves that exact record within the chat. Canonical delivery
  copies its exact text and source-message/conversation provenance into the child
  before admitting the assignment. Stable source keys prevent duplicate context
  after interrupted startup, restart and phase revisit. No ancestor transcript
  reader, native message role or second inbox/writer was introduced.
- Agent program tools submit through the existing retained-ID boundary and then
  wait at their call site. Batches submit all points before waiting. Terminal
  results include actual metrics/errors; immediate failures remain failures.
  The 30-minute wait deadline, interrupted observations and acknowledgement
  mismatches retain the original job IDs and never resubmit. HTTP and raw harness
  admission remain asynchronous; the workflow engine owns no polling policy.
- SDK reads use a 16,000-character page budget with accurate delivered bounds,
  complete-source `totalChars` and exact `nextArguments`, including character
  offsets within long lines. The hidden 400-line SDK limit is removed. Rust
  serving, Python source/templates and advertised schemas use the current page
  contract. Unsupported serving page metadata fails explicitly.
- Read failures are classified using trusted registered operation semantics.
  Read-only failures are safely repeatable observations with read-specific
  guidance; unknown-outcome mutations retain their reconciliation requirements.
  Missing committed-program diagnostics identify the requested program.
- Pure provider projection omits receipt arguments only when they exactly match
  the explicitly correlated native call. Canonical receipts, audit data, partial
  groups and checkpoint transcript evidence remain complete. Live and replay
  share projection `native-session-v6`.
- Managed target reconciliation distinguishes its own orders from protective
  stops and other explicit orders. Conditional exposure cannot satisfy a flatten
  request. Engine tests cover both directions, untriggered/triggered stops,
  partial fills, cancellation races and repeated closes without duplicate exits.

Look-ahead enforcement belongs to the separately owned change. This iteration
does not alter that implementation or launch a new strategy search.

## Verification

- `uv run --extra dev pytest -p no:cacheprovider -q`: **1,791 passed, 1 skipped**,
  161.77 seconds. The skip is the existing opt-in real-Cosmos latency acceptance.
- The final shared-dispatcher read-error refinement followed that full run.
  `tests/thesis/extensions/tools/test_job_reads.py` together with mandatory
  `tests/thesis_runtime/test_delivery_architecture.py`: **81 passed**. This also
  verifies trusted read semantics against a same-name non-read control. No third
  repository-wide Python run was made.
- Repository Ruff lint/format, `ty`, lockfile, LF and diff-whitespace checks pass;
  the final dispatcher refinement also passes focused Ruff/format/ty checks.
- `cargo test -p num -p backtest -p harness-strategy-jit -p mcp`:
  **131 passed** across 30 nonempty test binaries, including the new execution
  and SDK page regressions.
- The API `execution_pool` target was compiled with explicit installed MSVC and
  Windows SDK library search paths and its resulting test executable ran:
  **10 passed**, including retained identity, lost admission hints, interruption,
  ownership fencing and duplicate-capacity protections.
- `cargo fmt --all --check` passes. Focused all-target Clippy with warnings denied
  passes for num/backtest/strategy-jit/MCP and API/research. Mechanical Clippy
  corrections preserve the existing numeric oracle and prepared-replay tests;
  no warning suppressions or behavior changes were added for those corrections.
- Superseded prompt-substring and prompt-length assertions were removed from
  the existing conversational behavior test. That test retains tool-availability,
  operator-input and phase-transition checks; startup/revisit tests now assert
  actual canonical source identities and once-only context delivery. Existing
  receipt tests retain complete canonical arguments and check the smaller native
  projection separately. No mandated architecture or behavior suite was removed.

## Remaining verification boundary

The two full Rust workspace attempts failed during API linking with
`LNK1104: cannot open file 'msvcrt.lib'`, before workspace test execution. The
library exists. Loading Developer PowerShell alone did not resolve the full run;
explicit native library paths resolved the isolated API target. A full Rust
workspace pass is therefore **not claimed**. The permitted full-run retry was
used; subsequent checks were focused.

No persistent compiler configuration, local service restart, live job replay,
commit or push was performed. Existing unrelated pending changes and historical
verification records were preserved.
