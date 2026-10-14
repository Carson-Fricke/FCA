# SDK boundary and numeric behavior

This crate-local suite exercises the public authoring SDK without the engine,
lake, or JIT compiler. Dylib execution and persisted job artifacts are verified
in the neighboring strategy-jit and backtest crates.

- `abi.rs` checks lossless decimals, safe frame reconstruction, callback rejection,
  and panic containment across the public C shapes.
- `consumer.rs` checks dependency-first routing, canonical requirements, topology
  mutation, shared dependencies, and cycle rejection.
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
