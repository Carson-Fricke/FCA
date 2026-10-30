# SDK, read interfaces and execution-before-critique audit

## Scope and contracts

Audited the public authoring SDK and its shared contracts/numeric reexports,
host rejection adapters, serving SDK reference, Thesis program/knowledge/artifact
tools, research handoffs, retained submission attribution and async error delivery.
The current authoring contract is documented in
`docs/sdk-reference-and-diagnostics.md`, with constraints beside the SDK methods.

Research investigation is now `5.0.0`: proposals carry attempts/evidence and own
author/validate/run tools and the existing durable retention port from birth.
The phase prompt directs an initial executed trading policy before critique;
diagnostic branches execute their assigned study. It awaits delivered completion
conversationally, without polling. Repair rounds reuse valid immutable evidence
and execute only changed behavior. No phase, workflow gate or background-tool
delivery framework was added.

Attempts describe the current phase attempt's dispatched jobs. Evidence may also
cite prior retained branch executions or exact launch-brief evidence; branch job
attribution is checked against the immutable accepted input. Unknown submissions
retain their original ID and are reconciled, never replayed. Older workflow
versions are rejected for execution; historical evidence remains available for
the requested retrospective diagnosis. No old-contract decoder was added.

## Findings and repairs

| Audited surface | Finding / disposition |
| --- | --- |
| Research, SDK, program, knowledge and artifact readers | Replaced character limits/cursors with a real, identified cl100k_base tokenizer. Normal selections are returned whole, with 16,384 default / 65,536 maximum tokens. Larger selections expose exact UTF-8 token continuations and completeness. No new section-selector API. |
| Serving/local SDK projections | Updated Rust/Python producers, consumers, schemas and fixtures together. Line ranges remain explicit; both implementations use LF boundaries. Removed implicit program line caps and rejected outdated paging metadata. JSON string escaping counts toward the content budget; protocol metadata/provider accounting are separate. |
| Mutable read continuations | Program and research-state pages bind continuation to source/content hashes. Edited content cannot silently replace part of an earlier read. Discovery branch listings use the same read envelope rather than a separate capped result shape. |
| Tokenizer deployment | Added matching tiktoken/tiktoken-rs dependencies; both container recipes prefetch and copy Python tokenizer assets for offline runtime reads. |
| Capture API / host | Documented unprefixed dataset names and feature-name grammar, counts, finiteness, clocks, horizons and schema immutability. Split the aggregate validation error into specific failures, retaining dataset/symbol/frame and JSON field paths. Invalid captures still fail execution when author code ignores the return value. |
| Metric and output publication | Failures identify the metric/output and offending value, row or field. Publication validation preserves cross-output descriptor context. Reserved namespaces remain host-owned. |
| Order interface | Clarified queuing versus risk acceptance/fills, signed targets, units and immutable snapshots. Malformed cancel/target requests now retain a host rejection instead of disappearing when their SDK result is ignored. Ordinary risk rejection remains an execution outcome. |
| Numeric building blocks | Added input values/locations to diagnostics and explicit rejection of unrepresentable normalization, rolling/VWAP/Haar results and elapsed-time overflow. Rolling failures preserve prior state. Haar history rejects nonfinite energy before appending rows; RSI scales its ratio to avoid overflowing finite gains/losses. |
| Indicators / managed wavelets | Errors identify cadence, symbol, timing and parameter values. Documented warm-up/missing/undefined behavior and the actual trailing Haar implementation. Deliberate nullable/status-category APIs remain documented; exact host rejection detail is delivered with the job. |
| SDK exports | Embedded NULs in an error are escaped instead of replacing the diagnostic with `SDK error`. Corrected misencoded top-level SDK prose. |
| Thesis mutation interfaces | Preserved useful underlying harness/persistence causes, tool identity, retained job ID and outcome certainty. Read and mutation retry semantics remain distinct. |
| Knowledge interface | Distinguished model-repairable request failures from corpus/provider failures, preserving the existing private-path boundary. |
| Reference UI | Distinguished an excerpt with a continuation from a final excerpt; the last token page no longer tells the operator to fetch nonexistent remaining content. |
| Active documentation | Removed the obsolete single-bar-interval restriction and character-page descriptions. Updated architecture, package READMEs and method documentation; historical verification records were not rewritten. |

Public SDK modules reviewed: `abi`, `building_blocks`, `causal`, `consumer`,
`events`, `execution`, `export`, `indicator`, `lib`, `outputs`, `program`,
`wavelet`, and `research/{mod,publishing,recipes,view}`, together with the
subscription/capture contracts and checked decimal surface. Compiler/load
diagnostics and model-facing tool failure paths were traced through their
existing owners rather than introducing a parallel error service.

## Executable verification

- `uv lock --check`: passed.
- Repository Ruff lint and format checks: passed; 860 Python files formatted.
- Repository `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider --durations=10`: **1,799 passed,
  1 skipped, 134.16 seconds**. Includes the mandatory delivery architecture guard,
  arrival/audit/session suites and submission interruption/mismatch behavior.
  The skip is the existing opt-in live Cosmos latency test.
- The first serial integration run passed the same 1,799 tests but exceeded the
  three-minute budget at 223.54 seconds. The final run uses two isolated xdist
  workers with file-level scheduling as the canonical default; no coverage was
  removed or hidden behind markers. `-n 0` remains available for serial diagnosis.
- Native SDK/contracts tests and doctests: **44 passed**.
- Native compiled replay, mixed-input, SDK-reference and remote MCP suites:
  **7 passed**. These execute ignored capture/metric/output/order rejection cases
  and verify their exact diagnostics, not just validator strings.
- Cargo formatting across the workspace and Clippy with all targets for the six
  affected crates: passed.
- UI: **287 tests passed, 1 skipped**; production build passed.
- Offline tokenizer smoke: cl100k_base loaded successfully with the network-read
  function disabled, using its initialized local cache.
- `git diff --check` and repository LF check: passed.

Focused behavior additionally covers whole handoffs exceeding the retired
character cap, Unicode/JSON-escaped token reconstruction, source/state changes
between pages, exact program references reaching critique, evidence reuse,
proposal acknowledgement loss/mismatch without resubmission, and precise async
failure delivery after restart.

The retired pure character-page test was removed with that contract. Its
replacement behavior is exercised through SDK/program operations and HTTP
artifact reads, including Unicode and small token budgets. Pure source-digest
goldens remain.

## Local deployment verification

Rebuilt the optimized Harness API, execution worker and Lake ingestion worker,
then restarted Harness and Thesis with `scripts/local/start-stack.ps1`. Both
`http://127.0.0.1:8001/health` and `http://127.0.0.1:8000/health` returned
`{"status":"ok"}` from the new processes. The foreground helper reached its
readiness timeout during native compilation; its existing detached worker
continued successfully. No duplicate build or restart was launched.

A read-only smoke through the live MCP transport and production
`WorkspacePrograms` reader verified source identity and Python/Rust token
metadata agreement. `causal`, `program`, `consumer`, `wavelet`, `outputs` and
`building_blocks` each returned whole with the default budget (940, 5,747,
3,447, 2,268, 2,640 and 8,164 content tokens respectively). A 64-token request
returned an advancing continuation; a 31-token request returned
`invalid jit request: max_tokens must be between 32 and 65536`.

The UI test run and production build passed before deployment; the existing
local Vite server serves the updated reference-card component. No paid model
invocation or market-data strategy execution was initiated for this smoke.
The container cache-copy recipes were reviewed but container images were not
built in this local verification.
