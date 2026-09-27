# Harness Native Authoring Plan

<!-- markdownlint-disable MD013 -->

## Status

Wired. The SDK and JIT seam are proven; the embedded-Python authoring surface,
the `host`/`py` crates, and the harness Python packages are deleted; the
sweep interface is removed; Thesis emits authored Rust programs only and
reaches the harness exclusively through the harness-hosted MCP endpoint, the
HTTP API, and shared workspace files. All gates pass with no Python
interpreter configured for the harness (`cargo fmt/clippy/test --workspace`
plus `ruff`, `ruff format`, and the Python suite).

Operator direction (2026-09-26): the phase order was changed so the Python
deletion happens immediately after the SDK lands. The interpreter is no
longer tested through, the Python-driven tests are deleted rather than
maintained, and the repository gates must pass with no Python interpreter
configured. The reference-strategy ports follow the deletion instead of
preceding it. The `run_sweep` MCP tool and its local submitter/launcher were
removed with the sweep surface. Known follow-ups are the remaining N3/N4
content below: porting `mean_reversion`/`short_put_spread`, the Rust
screening program kind, and the workflow prompt refinements in
`prompts/creation.py` already authored above.

## Why

The embedded interpreter is the largest source of fragility in the harness:

* The MCP study path deadlocks on first interpreter use from a spawned
  thread (pinned by breadcrumbs to `Python::attach` + the first heavy
  extension import; unreproducible in test binaries, unresolved).
* Build-time interpreter binding is fragile: a release build without the
  venv pinned embeds the wrong Python and fails on 3.12 syntax with
  misleading errors; runtime needs the base interpreter directory on PATH
  or every process exits `STATUS_DLL_NOT_FOUND`.
* Per-event callbacks pay pyo3 conversion plus a GIL acquisition for every
  market event; a month of quotes is tens of millions of crossings.
* The interpreter boundary hid a silent synthetic-fixture fallback that let
  studies "succeed" on fabricated data (removed in 6b76e4c/5e24ffe).

The engine core is already native Rust. Strategies, indicators, and studies
are one implementation behind the `StrategyInstance`/`StrategyRuntime`
traits — Python is the replaceable part, not the architecture.

## Target shape

* **Authoring SDK** — `harness-strategy-sdk`: a small crate exposing the
  authoring traits (strategy, indicator, study program) over the engine's
  event and context types, with an entry-point macro. The SDK exposes only
  the capability surface the author is allowed: studies never see the order
  surface (same contract as `ResearchModelProgram` today).
* **JIT seam** — on submit, the platform writes the authored crate to a
  managed workspace, runs `cargo build --release` against the pinned
  toolchain, and loads the produced cdylib through `libloading`. The dylib
  exports one `extern "C"` factory returning the boxed runtime; the ABI is
  owned by the SDK so trait evolution is explicit. Compilation output,
  toolchain digest, and source digest feed the existing bundle-hash
  provenance flow.
* **Determinism** — identical source, toolchain, and configuration produce
  identical digests; the compile step is cached by source hash.
* **Thesis integration** — the MCP `run_study`/`submit_backtest` tools and
  the HTTP study route accept the authored Rust source (or a reference to a
  previously compiled artifact) exactly where they accept Python sources
  today; the model's edit-compile-run loop is served by the same submit
  surface.

## Migration phases

**Phase N1 — SDK and seam (done).** Built the `harness-strategy-sdk` crate
(author-facing traits, versioned C ABI, entry-point macros) and the
`harness-strategy-jit` seam (managed workspace with one shared `target/`,
`cargo build --release` against the pinned toolchain, `libloading` load with
ABI-version gating, source-digest caching). Proven by running an authored
study through the real engine job seam from a JIT-loaded dylib against a
Parquet lake, plus a strategy smoke run, the capability gate (a study
runtime has no order path by construction), panic containment, and metric
rejection handling.

**Phase N2 — Delete the embedded Python authoring surface now.** Remove the
PyO3 adapter, `python.rs`, the Python research adapter, the screening
sandbox, the Python indicator registry, the `py` extension crate, and the
harness Python packages; delete the Python-driven tests (synthetic golden
parity, HTTP parity through the interpreter) and the Python example
strategies. The serving paths (MCP, HTTP, CLI) run only JIT-compiled
Rust-authored sources: a configuration names `implementation: "jit"` and
carries the authored source, whose digest feeds the existing bundle-hash
provenance flow. Exit: `cargo fmt/clippy/test --workspace` passes with no
`PYO3_PYTHON` and no interpreter on PATH.

**Phase N3 — Port the reference strategies.** Port `strategies/harness_examples`
(intraday momentum first) to the SDK as authored Rust programs and re-pin
their behavior with lake-run tests asserting event/decision counts.

**Phase N4 — Thesis produces Rust implementations only, over hosted
surfaces.** Thesis is the authoring platform, so the Python exit is not a
transport change: every implementation Thesis produces becomes authored
Rust. Research models store and emit authored Rust study programs
(`implementation: "jit"`, source in `parameters`, bundle hash equal to the
source digest) — the Python `model_source`/`fit`/`observe` plumbing and the
indicator-binding schema are deleted. Strategy bundles persist Rust sources
and hash those. Screener filters become Rust-authored programs or the
feature pauses until ported. The hypothesis-workflow phase prompts author
Rust against the SDK, and the model's edit-compile-run loop is the JIT seam
itself: submit, read the compiler or engine failure, revise. No Python
source or Python implementation string ever enters a submitted
configuration, and the workflow tests assert the Rust emission.

Integration topology (operator direction, 2026-09-26): the only substantive
Thesis→harness channels are the harness-hosted MCP endpoint and the harness
HTTP API; file artifacts in the shared workspace are the only filesystem
overlap, readable by both sides. Thesis never spawns harness processes: the
stdio MCP transport (Thesis spawning `harness mcp`) and the local
`backtest-job`/`trader` process-exec boundary are deleted, and every launch
and status query flows through the hosted MCP tools or the REST routes.

## Risks

* **Dylib ABI stability** — trait objects do not cross `extern "C"`
  boundaries; the SDK owns a C-ABI factory plus a stable vtable shape, and
  the SDK crate version is compiled into every authored artifact.
* **Compile latency** — a small authored crate compiles in seconds with a
  warm target; the managed workspace keeps one shared `target/` so only the
  author's crate recompiles.
* **Capability leakage** — the SDK must not re-export engine internals that
  would let an authored crate reach the order surface in a study; the
  capability test suite from the Python gate moves to the SDK.
* **Authoring ergonomics** — model authors lose the Python REPL loop; the
  compile-cache and the bar-feed (see below) are what keep iteration fast.

## Bar feeds (adjacent, unchanged)

`data.bar_interval` already exists in the backtest configuration schema but
nothing implements it. Deriving 1-minute (or 1-second) bars natively from
fetched tick partitions — as a derived lake data type or a query-time view —
shrinks study replay by two to three orders of magnitude and is the
prototyping-speed lever regardless of authoring language. Tick replay stays
for final validation. Explicitly out of scope for the N-phases above until
the authoring surface lands.

## Out of scope

* WebAssembly as the authoring target (revisit only if capability
  sandboxing in-process is ever required beyond compile-time SDK limits).
* Python authoring via subprocess/IPC workers (the embedded path is retired,
  not relocated).
