# Discovery sketch and executable tool bag

Scope: discovery, its assumption-oriented falsification/verdict handoffs,
causal multi-timeframe bars, exploratory outputs, knowledge recipes, and UI
inspection. Deployment remains separate.

## Implemented boundaries

- Workflow `3.0.0` develops a committed strategy sketch before reading prior
  study findings. Subsequent experiments revise its mechanism, representations,
  conditioning, entry/exit/abstention and named assumptions. Accepted handoffs
  resolve the exact sketch hash and Git commit, including pinned components.
- Falsification binds tests to sketch assumptions. Verdict assesses every
  assumption with actual executed tests and explicit untested/inconclusive
  states. Strategy construction receives the accepted immutable sketch.
- Knowledge cards teach executable composition and method limitations. SDK
  building blocks include directional change, important points, a causal Haar
  transform, rolling moments, common indicators, crossover timing and frozen
  normalization. Autoencoders and other transforms require authoring; they are
  not advertised as shipped SDK functions.
- SDK ABI `5` publishes series, confirmed events, scalar tables and fitted
  state. The execution host owns fitted-state job provenance and validates its
  training window. Jobs persist composed indicator sequences automatically.
- One canonical minute acquisition supplies completed session-anchored `Nmin`,
  `Nh` and daily bars. Slower completed frames arrive before faster frames at
  the same availability time. Missing constituent bars cannot manufacture a
  partial completed bar. Strategies, studies, indicators and screeners consume
  multiple declared cadences; tick streams retain their original semantics.
- HTTP and agent inspection project the same immutable exploratory artifact.
  UI inspection provides paged records, a series view and complete export.
  Sketch UI shows revisions, components and authoritative job references.

## Verification discipline

The first integration run identified obsolete single-timeframe assertions and
HTTP fixtures carrying the superseded discovery completion document. Those
were replaced with the accepted sketch contract. The source guard, canonical
delivery behavior and job-identity suites remain executable and required.

Removed `test_session_registry_forwards_the_policy_to_program_tools`: it tested
internal registry wiring with an incomplete domain fake, outside the permitted
unit categories. Real registry-to-submission behavior remains covered by
`tests/thesis/http/test_study_validation_recovery.py` with committed sketches.

Historical verification records were not rewritten. Pre-existing unrelated
working-tree changes were retained.

## Final verification

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: 556 files passed.
- `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider`: 1,612 passed, one live Cosmos
  latency check skipped, one warning, **157.46 seconds**. This was the single
  permitted final full rerun after repairing the initial integration failures.
- `cargo fmt --manifest-path src/harness/Cargo.toml --all -- --check`: passed.
- Workspace Clippy passed. Rust execution continued by package after fixing
  obsolete interval assertions: backtest passed; API strategy validation passed
  all three cases, study validation passed; the remaining workspace packages
  passed with `cargo test --manifest-path src/harness/Cargo.toml --workspace
  --exclude api --exclude backtest`. New lake acquisition and actual JIT replay
  checks passed for simultaneous one-minute, five-minute and daily bars.
- UI: `npm run check` reported zero errors/warnings; Vitest reported 266 passed
  and one skipped; `npm run build` passed. Check/build passed again after the
  final artifact link and empty-cadence form correction.
- Knowledge corpus validation passed, and the executable knowledge-card study
  recipe compiled against the actual SDK.

Live acceptance uses the restarted master-checkout harness, Thesis API and UI.

## Live acceptance and final focused repairs

The first fresh conversation launched `3.0.0`, committed
`msft_compression_release_v1` before reading prior study contents, authored and
validated a study, and submitted `job-f9489c4a44ea4abea604a8b5b7ba2890`.
Its workflow later failed on a local Cosmos read timeout; the successful harness
job was retained and inspected, never replayed. Restarting the emulator retained
its named data volume. No database retry or distributed-storage mechanism was
added.

Live inspection found missing harness-data composition in both serving
entrypoints. Both now supply `HarnessDataService` explicitly. The completed
job's HTTP routes return 200, with 11,532 confirmed events and 4,000 sequence
values. Browser inspection verified the sketch graph, assumptions, revisions,
sequence view and record paging. Complete export returned 6,742,168 bytes with
SHA-256 `6ebb3f0b1abf6b48495c1bbc08a099056725d461644b7b5e1a5b00657c6cf255`,
matching the canonical output catalog.

Fresh conversation `chat-f1694a20f9ad4ccf961d71d536039ca6` launched execution
`execution-e3527914145a41848d442ec13c0defaf`. It committed its initial sketch,
successfully executed two studies, inspected event outputs, revised the sketch,
and requested operator review from inside discovery. A smoke-test request for
older dates was excluded by the launcher's chosen partition; discovery reported
that conflict and preserved the frozen boundary. Launcher guidance now explicitly
explains the launch-time anchor and requires historical dates to fall inside
development after subtracting the sealed tail and purge. Prompt guidance is not
proof of model compliance.

Final acceptance also binds every sketch evidence job to validated discovery
attempts. Supported/refuted assumptions require nonempty evidence from succeeded
jobs; provisional authoring stays exploratory. Six additional boundary cases
passed in the 13-case sketch handoff suite. Large fitted-state inspections fail
with an explicit complete-export instruction instead of emitting unbounded tool
responses. Combined output/handoff verification passed 16 cases in 2.45 seconds.
Delivery architecture and HTTP output checks passed 58 cases in 4.74 seconds;
generated-wire and sketch checks passed 21 cases. Focused Ruff, format and ty
checks passed after these final changes. The full suite was not rerun again.

The final server restart stopped the review-stage smoke workflow under the
existing runtime restart policy; immutable evidence remained accessible.
Final conversation `chat-253100908b6f499093d82a6e7ca91b69` launched execution
`execution-5816795896dd4c4da1918de7cf2fadfa` on the final code and entered
discovery. Its explicit 820-day history and 45-day sealed tail include the
requested November 2024 development investigation, confirming the corrected
launcher guidance in this live example. Delivery architecture and market-input
checks passed 115 cases in 3.12 seconds after the guidance change. Servers are
left running from the master checkout; no further restart is performed.
