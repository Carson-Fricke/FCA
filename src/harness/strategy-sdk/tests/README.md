# SDK boundary and numeric behavior

This crate-local suite exercises the public authoring SDK without the engine,
lake, or JIT compiler. Dylib execution and persisted job artifacts are verified
in the neighboring strategy-jit and backtest crates.

- `accounting.rs` checks partial fills, duplicate/conflicting fill identities, shorts,
  reversals, entry-notional returns and explicit quantile/slope/Haar definitions.
  Engine-owned accounting and context-driven order behavior are exercised by the
  research crate's managed_execution authored-program behavior test.
- `abi.rs` checks lossless decimals, safe frame reconstruction, callback rejection,
  and panic containment across the public C shapes.
- `consumer.rs` checks dependency-first routing, canonical requirements, topology
  mutation, shared dependencies, and cycle rejection.
- `routing.rs` checks exported callback mutation rejection and permanent failure,
  canonical-equivalent declarations, and a steady-state allocation budget that
  rejects per-event graph reconstruction.
- `execution_views.rs` checks demand-driven host queries and independently
  memoized account, order and closed-trade views.
- `indicator_sequence.rs` checks bar/tick output fidelity, causal completion,
  missing states, and per-symbol history.
- `exploratory_outputs.rs` checks numeric feature warm-up, directional confirmation,
  trailing shape/scale math, frozen fitting, and structured evidence validation.
- `wavelet.rs` verifies dependency-routed multi-timeframe availability boundaries,
  symbol isolation, missing/warm-up resets, prefix/future-perturbation invariance
  and immutable historical snapshot lookup.
- `research_views.rs` checks complete-publication references and field semantics,
  explicit identities, causal timing, family compositions and actual Haar history.
  Standalone semantic descriptors are checked without an exhibit, including
  duplicate grain, invalid roles, strict decoding and conflicting view bindings.

- `diagnostics.rs` checks exact bad output locations, parameter diagnostics, nonfinite arithmetic rejection, transactional rolling state, and elapsed-time overflow.
