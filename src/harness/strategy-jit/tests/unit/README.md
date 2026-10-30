# JIT implementation tests

These path-included tests exercise private compiler, diagnostic, provenance,
validation and workspace contracts. Authored ABI and public reference behavior
are exercised by the integration tests in the parent directory.

- `compile.rs` checks compiler diagnostics and immutable generation identity.
- `diagnostic_compactness.rs` checks bounded structured diagnostic projection.
- `diagnostics.rs` checks sandbox-source validation.
- `fixture.rs` supplies authored fixture sources for compiler tests.
- `execution_views.rs` checks lazy host serialization-cache transitions without
  eager encoding or invalidation of unchanged histories.
- `provenance.rs` checks compiler fingerprint inputs and invalidation.
- `sdk_reference.rs` pins cross-language SDK source canonicalization and real changes.
- `validation.rs` checks validation-cache outcomes and bounds.
- `workspace.rs` checks workspace paths and locked manifest updates.
