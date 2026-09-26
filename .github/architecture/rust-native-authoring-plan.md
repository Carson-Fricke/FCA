# Harness Native Authoring Plan

<!-- markdownlint-disable MD013 -->

## Status

Approved direction, not started. This plan replaces the embedded-Python
authoring surface (PyO3 `harness-host` adapter, Python strategies,
indicators, screeners, and research models) with Rust-authored strategies,
indicators, and studies that are JIT compiled when a model submits one and
loaded behind the existing native engine seams.

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

**Phase N1 — SDK and seam.** Build the SDK crate and the JIT compile/load
seam; prove it by running one study through the engine from a JIT-loaded
dylib. No Python removal yet.

**Phase N2 — Port the reference strategies.** Port
`strategies/harness_examples` (intraday momentum first) to the SDK and pin
behavior against the existing tests (lake runs assert event/decision
counts, not byte parity with the synthetic benchmark).

**Phase N3 — Flip the authoring surface.** MCP/HTTP study and backtest
submission require Rust-authored sources; the Python adapter remains only
for the legacy example suite until ported.

**Phase N4 — Delete the embedded interpreter.** Remove `harness-host`'s
PyO3 adapter, `python.rs`, the research adapter, the screening sandbox, and
the Python indicator registry; the runtime venv stops being a harness
dependency (it stays for the lake's retained surface and thesis tooling).

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
