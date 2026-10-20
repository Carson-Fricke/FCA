# Compile authored programs and adapt their callbacks

This folder bridges authored SDK programs to the native harness. Requirements
are read from constructed program state before lake fetches. Engine-owned
replay and lake planning remain outside this crate. All market calls use SDK
subscription filtering, including direct runtime calls in tests.

- `lib.rs` exposes compilation, loading, runtime, and configuration entrypoints.
- `compile.rs` coordinates cache reuse and compilation under per-compiler/source
  OS file locks. Each build attempt uses unique crate and artifact directories;
  returned libraries are never replaced or removed by later builds. Cargo
  dependency artifacts are reused in a target directory keyed by the complete
  compiler fingerprint. An additional OS lock serializes writes to that target;
  the final library is copied into its immutable generation before releasing
  ownership. Changed SDK content, tools, configuration or ABI provenance select
  a different dependency target, including changes that preserve mtimes.
- `compiler.rs` writes authored crates and invokes Cargo, resolving the emitted
  library from Cargo artifact records even when a build target is configured.
- `diagnostics.rs` classifies typed Cargo JSON records and retains compact
  authored errors, locations, labels, replacements, and child suggestions.
  Structured diagnostics omit rendered copies, source excerpts, and long code
  explanations. Rendered text does not determine cacheability.
- `provenance.rs` fingerprints SDK sources, compiler tools, Cargo configuration,
  and environment. Rustup settings changes invalidate cached probes. The sibling
  `build.rs` embeds a hash of the complete JIT source tree, manifest, and build
  script, together with the host target, using a direct SHA-256 build dependency.
- `validation.rs` owns the full-input validation cache (64 entries and 4 MiB).
  It covers loading and construction; infrastructure failures are never memoized.
  Failures point to compact diagnostics or a minimal SDK missing-export template;
  `for_program` tailors study guidance to strategy traits, context, exports, and
  the `validate_strategy` tool. It does not rewrite or retry authored source.
- `config.rs` constructs configured study or strategy instances.
- `error.rs` defines compilation, loading, and authoring errors.
- `load.rs` loads libraries, validates ABI handshakes, and owns program states.
- `run.rs` validates configurations, exposes effective requirements through
  `configuration_subscriptions`, checks study echoes through
  `inspect_configuration`, reports compilation through the injected progress
  port, and hands constructed instances to engine jobs.
- `runtime.rs` adapts subscribed quotes, trades, interval-identified atomic
  frames, metrics, and strategy orders. Frame orders are returned by
  `on_frame_orders` and stamped at frame availability. The observation-only
  strategy `on_bar_frame` entrypoint rejects dispatch rather than dropping orders.
- `screen.rs` executes and ranks one-shot screening results.
- `screen_run.rs` runs screening configurations and writes their artifacts.
- `source.rs` provides lake-backed screening data access.
- `toolchain.rs` resolves the pinned compilation toolchain.
- `workspace.rs` resolves workspace roots to canonical absolute paths and
  manages authored workspaces and artifact manifests. Manifest entry updates lock
  the complete read/merge/atomic-replace operation across processes. On Windows, verbatim disk
  and UNC prefixes are converted to conventional paths accepted by MSVC.

Focused tests and authored fixture sources live in the sibling `tests/` folder.
`tests/validation_guidance.rs` covers compact failures and the minimal study
contract; `tests/unit/diagnostic_compactness.rs` covers retained locations and
suggestions without duplicate rendered payloads. Test fixtures are not runtime
authoring defaults or evidence that a research hypothesis has been implemented.

Workspace program IDs and Git publication belong to Thesis, not this crate.
The JIT compiles the assembled source/configuration it receives. Thesis's packaged
SDK reference identity includes the canonical SDK, harness contracts, num, and
lake contracts Cargo/source
hashes; JIT compiler provenance additionally covers its own build and toolchain
environment. These identities have different owners and are not interchangeable.

Managed workspaces embed the harness `rust-toolchain.toml`; authored builds use the same pinned compiler shipped in the container.
