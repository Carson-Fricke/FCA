# Replay-sealed observations — 2026-10-28

## Implemented contract

Studies and strategies share `MarketConsumer::on_decision` and a borrowed
`DecisionContext`. The host invokes this after ordinary dependency-first bar
routing. Capture requests contain a dataset, present symbol, finite feature
vector and positive elapsed horizon. They contain no authored timestamp or
observation identity. The host copies features immediately; no row update or
outcome-read capability is exposed, and final publication receives no capture
port. A dataset's feature schema and horizon are immutable. Duplicate captures,
absent symbols, invalid features, invalid/overflowing horizons and ignored
capture rejections fail execution.

The host resolves close-to-close returns in basis points only at the exact
future availability on the same symbol and cadence. Missing observations do not
use later prices. Remaining horizons at stop retain `end_of_run` with null
outcomes. Returns exclude execution costs. Pending horizons are indexed rather
than rescanning historical rows on every frame.

Only the host may publish the `causal_observations` variant or names under
`causal/`. Publication merges sealed observations before resolving saved-view
references. Authored tables/series/fitted state remain exploratory and the
inspector distinguishes publication authority. Python and UI page the canonical
nested table; clocks and horizon metadata preserve decimal strings. The study
template captures observations and `read_sdk_reference(topic="causal")` exposes
the current authoring API through the harness and Thesis.

This is a guarantee for supported replay/capture inputs. Native Rust remains
outside a security sandbox. External or hard-coded future knowledge, incorrect
source availability, training leakage and research selection bias require
separate controls. An authored retrospective table cannot acquire sealed
provenance, but its numeric claims are not independently proved.

## Cutover

SDK ABI is 8. Existing compiler identity and loader checks rebuild or reject
older compiled layouts. No compatibility callback path or read-time conversion
was added. Existing exploratory artifacts remain exploratory; no workspace,
job or conversation data was deleted. Serving processes must use a rebuilt
harness before they expose this capability; this task did not restart active
services or jobs.

## Focused verification

Commands below run from the repository root, except Cargo commands run from
`src/harness`. Native final checks used the installed Visual Studio
`vcvars64.bat` environment after the initial full build reported missing
`msvcrt.lib`.

- `cargo test -p harness-research --test causal_observations`: passed. One
  behavior test exercises 24 real replay invocations across study/strategy
  programs: prefix truncation, future-price perturbation, stable prior
  features/orders, delayed/missing/end-of-run labels, stop-time mutation,
  saved-view references, frozen schemas/horizons and valid-looking forgeries.
- `cargo test -p harness-strategy-sdk`: 38 tests passed, including the
  compile-fail check preventing capture-capability retention.
- `cargo test -p harness-strategy-jit --test compile --test sdk_reference`:
  four tests passed, including ABI rebuild and current causal source discovery.
- `cargo clippy -p harness-contracts -p harness-trading -p harness-strategy-sdk -p harness-strategy-jit --all-targets --no-deps -- -D warnings`:
  passed for the affected crates.
- `uv run --extra dev pytest -p no:cacheprovider tests/thesis/http/test_exploratory_outputs.py tests/thesis_runtime/test_delivery_architecture.py -q`:
  86 passed, including the mandatory delivery architecture guard.
- `npm run test -- src/tests/research-contracts.test.ts` from `src/ui`:
  eight passed, including multi-page sealed observations and exact timestamps.
- Focused Ruff lint/format and `ty check` passed for the changed Python files.
  Owned Rust files were formatted; `git diff --check` and the repository LF
  check passed. The SDK-generated gallery was regenerated through its normal
  production paging projection.

## Repository-wide verification limits

One integration pass ran the canonical lock, Ruff, formatting, ty, pytest,
Cargo formatting, Clippy and native test commands. The lock check passed.
This workspace is not globally green:

- Ruff reported four errors outside the causal implementation, including an
  undefined `_model_creation_turn_prompt` export and missing `decode_activity`.
- `ty check` reported five diagnostics in workflow prompts/runtime and subagent
  specs. Repository Python formatting reported 16 files; Cargo formatting also
  reported existing differences outside the causal implementation.
- Workspace Clippy stopped at `num/src/decimal.rs:374`
  (`manual_is_multiple_of`). The numeric kernel was not changed by this task.
- The full Python suite collected 1,769 tests, reported unrelated workflow
  failures, and was stopped at the required 180-second bound. The active
  `tests/thesis/http/test_context_compaction.py` file was then isolated with a
  hard timeout: all 18 tests passed in 7.71 seconds. The unchanged full Python
  suite was not rerun.
- The initial native workspace build failed linking `api`'s `backtest-job`
  because the shell lacked `msvcrt.lib` lookup paths. All focused native checks
  above passed after initializing Visual Studio's build environment.

The one final native workspace retry after that environment correction was
stopped at the 180-second bound during compilation, before test execution.
No further full native run was attempted. Its result is recorded in
`.local/reviews/causal-verification/cargo-test-final.json` and its log.
Detailed integration and focused logs are retained in that ignored local
directory. Shared SDK-reference/tool files also received concurrent unrelated
edits during verification; causal entries were retained and final focused
source-discovery checks passed. No claim of a clean repository-wide run is made.
