# SDK publication examples

- `research_gallery.rs`: generates deterministic, explicitly synthetic evidence
  and validated saved views plus independent dataset semantics for nine study
  families using the public SDK. Parameter experiments include a third dimension
  for exercising ambiguous heatmap mappings and explicit slices.
  The wavelet example records actual trailing Haar coefficients and scale energy.

Run through `cargo run --manifest-path src/harness/Cargo.toml -p
harness-strategy-sdk --example research_gallery`. The fixture generator under
`scripts/fixtures` pages this artifact through the production HTTP projection.
