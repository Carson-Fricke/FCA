# Harness Native Authoring Plan

<!-- markdownlint-disable MD013 -->

## Status

`harness-contracts` is now a workspace member under `src/harness/contracts/`.
It owns dependency-light tagged subscriptions, data-type/interval vocabulary,
canonical subscription normalization, and configuration hashing. The SDK depends
on contracts and `num`, not `backtest`; configuration and engine adapters depend
toward those contracts. Canonical interval vocabulary accepts positive
`s`/`min`/`h`/`d` units; the engine's supported `1min`/`1d` intervals and
single-bars-interval restriction remain separate execution policy.

The shared `config` crate owns YAML/JSON syntax, shell/replay schema validation,
and typed normalization reused by API, MCP, CLI, and engine projection. There is
one YAML parser; `backtest` converts the validated tree into engine decimal/time
types instead of keeping another parser or replay schema. One contracts hasher
preserves explicitly selected historical ASCII-escaped and UTF-8 v1 encodings.
Submitted-document hashes omit resolved defaults and remain distinct from resolved
engine hashes. Unicode encoding, committed configurations, and nanosecond-bound
identity have golden/regression coverage; persisted hashes are not re-pinned.

The authoring SDK now uses ABI v4 and a shared `MarketConsumer` interface for
studies, strategies and program-owned indicators. Subscriptions declare exact
symbols and `bars`/`quote`/`trade` inputs; only bars require an interval. The
compiled union includes recursive indicator dependencies and drives study
ensure/read requests without extra types. Callback routing stays consumer-local
and dependency-first. Studies/indicators use `ObservationContext`; strategies
use `OrderSink`. See the current contract below and `docs/strategy-authoring-guide.md`.

Compiler identity and validation reuse are implemented in the Rust JIT service,
not the Thesis transport. The local `StudyValidator` delegates to a bounded
64-entry, 4 MiB full-input cache, returning server-owned reuse/cacheability and Cargo
JSON diagnostics. MCP consumes an injected validator's optional opaque identity
without filesystem inspection or compiler probes. Python has no validation LRU,
regex diagnostic inference, or compiler-identity probing. See the validation
boundary below; these changes do not claim every remaining authoring task is done.

Wired. The SDK and JIT seam are proven; the embedded-Python authoring surface,
the `host`/`py` crates, and the harness Python packages are deleted; the
sweep interface is removed; Thesis emits authored Rust programs only and
reaches the harness exclusively through the harness-hosted MCP endpoint, the
HTTP API, and shared workspace files. Earlier native-authoring integration
verified the harness with no Python interpreter configured
(`cargo fmt/clippy/test --workspace` plus `ruff`, `ruff format`, and the Python
suite); that historical result is not fresh verification of the current worktree.

Operator direction (2026-09-26): the phase order was changed so the Python
deletion happens immediately after the SDK lands. The interpreter is no
longer tested through, the Python-driven tests are deleted rather than
maintained, and the repository gates must pass with no Python interpreter
configured. The reference-strategy ports follow the deletion instead of
preceding it. The `run_sweep` MCP tool and its local submitter/launcher were
removed with the sweep surface. Known follow-ups are the remaining N3
content below: porting `mean_reversion`/`short_put_spread` and the workflow
prompt refinements in `prompts/model_creation.py` already authored above. N4 is
delivered, including the Rust screening program kind: screener filters are
Rust-authored `ScreeningProgram` programs submitted through the harness MCP
(see `screener-rust-migration.md`). The path that turns a surviving hypothesis
into a workspace Rust strategy — the `author_strategy` tool and the
`strategies/<slug>/<version>/` artifact — is `strategy-authoring-plan.md`.

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
  authoring traits (strategy, indicator, study, and screening program) over
  author-facing event and context types, with an entry-point macro. It depends
  toward `harness-contracts` and `num`, not the backtest engine. The SDK
  exposes only the capability surface the author is allowed: studies never see
  the order surface (same contract as `ResearchModelProgram` today) and fold
   quotes, trades and atomic `BarFrame`s through `MarketConsumer` callbacks,
   and screening programs see only the
  `ScreenContext` cross-section.
* **JIT seam** — on submit, the platform writes the authored crate to a
  managed workspace, runs `cargo build --release` against the pinned
  toolchain, and loads the produced cdylib through `libloading`. The dylib
  exports one `extern "C"` factory returning the boxed runtime; the ABI is
  owned by the SDK so trait evolution is explicit. Compilation output,
  toolchain digest, and source digest feed the existing bundle-hash
  provenance flow.
* **Determinism** — source digest identifies authored content; artifact reuse
  additionally checks program kind, SDK version/ABI, toolchain, compiler
  fingerprint, and artifact existence. Source hash alone is not compiler identity.
* **Thesis integration** — the MCP `run_study`/`submit_backtest` tools and
  the HTTP study route accept the authored Rust source (or a reference to a
  previously compiled artifact) exactly where they accept Python sources
  today; the model's edit-compile-run loop is served by the same submit
  surface.

## Migration phases

**Phase N1 — SDK and seam (done).** Built the `harness-strategy-sdk` crate
(author-facing traits, versioned C ABI, entry-point macros) and the
`harness-strategy-jit` seam (managed workspace with isolated build generations,
`cargo build --release` against the pinned toolchain, `libloading` load with
ABI-version gating, source-digest caching). Proven by running an authored bar
study through the real engine job seam from a JIT-loaded dylib over the
market-lake bar read route, plus a strategy smoke run, the capability gate (a
  study runtime observes subscribed market inputs and has no order path),
panic containment, and metric rejection handling.

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
surfaces (done).** Thesis is the authoring platform, so the Python exit is not a
transport change: every implementation Thesis produces becomes authored
Rust. Research models store and emit authored Rust study programs
(`implementation: "jit"`, source in `parameters`, bundle hash equal to the
source digest) that implement `MarketConsumer` and declare exact
`harnessConfiguration.subscriptions`, emitted as `data.subscriptions` and
checked against the compiled dependency union. Bars declare `1min` or `1d`;
tick-only studies require no global interval. Persisted interval-only
configuration stays readable, but authored sources using the older callbacks
must migrate to `MarketConsumer` and rebuild for ABI v4. Studies publish their
Layer A observables as metrics — the Python `model_source`/`fit`/`observe` plumbing
and the indicator-binding schema are deleted. Strategy bundles persist Rust
sources and hash those. Screener filters are Rust-authored `ScreeningProgram`
programs (SDK `ScreeningProgram` and `export_screening_program!`) submitted
through the harness MCP: the harness JIT-compiles them, builds the screening
context from the lake routes, ranks the returned rows, and writes the
`screen.json` artifact, while Thesis owns the definitions as content-addressed
workspace artifacts. The design of record is `screener-rust-migration.md`.
The hypothesis-workflow phase prompts author Rust against the SDK, and the
model's edit-compile-run loop is the JIT seam itself: submit, read the
compiler or engine failure, revise. No Python
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
* **Compile latency and disk growth** — provenance-matched artifacts can be
  reused, but every cache miss builds in a distinct crate/target generation.
  This protects libraries already in use at the cost of additional builds and
  disk use. Generations are not automatically garbage-collected.
* **Capability leakage** — the SDK must not re-export engine internals that
  would let an authored crate reach the order surface in a study; the
  capability test suite from the Python gate moves to the SDK.
* **Authoring ergonomics** — model authors lose the Python REPL loop; the
   compile-cache and the bar-feed (see below) are what keep iteration fast.

## Validation Boundary (Implemented)

`strategy-jit/src/compile.rs` remains the public compile/cache coordination
facade and re-exports validation entrypoints. `compiler.rs` owns authored-crate
generation and Cargo execution; `provenance.rs` owns compiler fingerprints and
cached successful probes; `diagnostics.rs` owns Cargo diagnostic classification;
`validation.rs` owns `ValidationService`, `ValidationFailure`, and bounded reuse.
`toolchain.rs` resolves the selected toolchain through the probe service.
`build.rs` embeds a deterministic identity of the JIT manifest, build script,
complete source tree, and host target, without needing that tree at runtime.
Compiler identity covers compiled JIT implementation, current SDK/num source
bytes, Cargo/toolchain configuration, relevant environment, and resolved compiler
executables. Probe reuse freshly checks configuration and executable metadata;
failed probes are not cached. An identity change invalidates validation reuse and
the actual compiled artifact, including its immutable generation-specific path,
not merely an advertised token. Artifact reuse still requires the expected
kind, SDK version/ABI, toolchain digest, fingerprint, and existing dylib.

`workspace.rs` uses kernel-owned OS file locks for setup, compiler/source/kind
build exclusion, and manifest merging. Independent source builds do not share
one mutable Cargo output tree. Each cache miss creates a distinct authored crate
and target generation; manifest entries merge under a short lock and publish by
atomic replacement. Completed artifact paths are never overwritten by another
build, and generations are not automatically deleted while callers may load them.
Locks release on close/process exit; lock files are not unlinked. Standard-library
`File::lock` requires Rust 1.89 or newer. The implementation contains portable OS
locking and Windows compiler-path handling; Linux behavior was not tested in this
change, so this document does not claim cross-platform verification.

`api::local::NativeStudyValidator` uses the process-shared JIT validation service
for compile/load/construction and configuration/subscription checks. Its cache
holds at most 64 results keyed by the full model input and compiler identity.
Successful results and deterministic rejections may be cached; infrastructure
failures are not. Missing identity disables reuse, and identity is checked again
after inspection before publishing a cache entry.

Cargo's JSON `compiler-message` records establish authored-source diagnostics.
Dependency/linker/infrastructure errors are not classified by matching rendered
text. Results expose `reused`, `cacheable`, `deterministicCompilerDiagnostic`,
diagnostics, and optional `validationIdentity`. A deterministic configuration
rejection may be cacheable without being a compiler diagnostic; only authored
compiler diagnostics justify asking the model to repair Rust source.

The `mcp::tools::StudyValidator` port allows injected validators to provide an
optional opaque identity. MCP forwards it as `serverInfo.validationIdentity`
and forwards validation metadata without knowing compiler layout, inspecting
filesystem sources, or running probes. Thesis's `HarnessToolProvider` caches the
successful tool catalog only; Python transport has no validation LRU, regex
compiler classification, or identity probing. Completion-time validation uses
the same server validator and the conversation's runtime-internal activity writer.

## Market consumers and feeds (delivered)

`MarketConsumer` owns `subscriptions(&self) -> Vec<Subscription>`,
`indicators(&mut self) -> Vec<&mut dyn Indicator>`, and `on_quote`, `on_trade`,
and `on_bar(&BarFrame, &mut Self::Context)` callbacks. Studies and SDK indicators
use `Context = ObservationContext`; strategies use `Context = OrderSink`.
`StudyProgram` retains `new(parameters)` and `on_stop(&mut MetricPublisher)`;
`StrategyProgram` additionally retains `on_start` and `on_fill`. SDK indicators
add `Indicator::value` and expose references to their owned nested dependencies.
The native built-in indicator registry has not migrated to this interface.
Requirements and topology must remain stable after construction; cycles and
zero-sized indicator dependencies are rejected. Routing updates dependencies
before their parent and delivers only each consumer's own types and symbols.

Subscriptions are `Bars { symbols, interval }` (`1min` or `1d`),
`Quote { symbols }`, or `Trade { symbols }`. ABI v4 exports the constructed
source-and-dependency union as tagged JSON before acquisition. Thesis preserves
`harnessConfiguration.subscriptions` as `data.subscriptions`, which must
canonically equal that union; `validate_study` returns the compiled union.
Only one distinct bars interval is supported per run, including dependencies
and program instances; conflicts fail before lake ensure/read. Tick-only inputs
need no global interval. Legacy interval-only documents remain readable, not
legacy source APIs or compiled ABI v3 artifacts.

Studies ensure/read exactly their effective requirements through lake routes.
Bars are provider-first, with derivation from already stored trades otherwise;
a bars ensure never fetches undeclared trades. Backtests additionally acquire
quote/trade execution feeds. The manifest records `effective_subscriptions`,
`execution_subscriptions`, and their deduplicated `acquired_subscriptions` union.
Execution feeds neither enlarge consumer callbacks nor belong in the config echo.
Mixed replay uses one causal availability queue: ticks precede completed frames,
then generated execution events at equal time. `on_bar` receives atomic frames
at interval end, never raw single bars or bar-start lookahead. Bar subscriptions
can keep statistical replay small without restricting studies to bars or
strategies to ticks.

## Out of scope

* WebAssembly as the authoring target (revisit only if capability
  sandboxing in-process is ever required beyond compile-time SDK limits).
* Python authoring via subprocess/IPC workers (the embedded path is retired,
  not relocated).
