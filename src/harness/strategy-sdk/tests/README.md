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
