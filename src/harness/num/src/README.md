# Exact numeric primitives

This folder contains pure checked numeric kernels; it has no storage, transport,
job or provider dependencies. Decimal conversion and rounding belong here rather
than passing through text in replay or reporting.

- `lib.rs`: exports the numeric facade.
- `decimal.rs`: implements variable-scale decimal arithmetic and float boundaries.
- `decimal18.rs`: implements checked fixed-scale arithmetic, parsing and direct variable-scale conversion with the same half-even and overflow rules.
- `wide.rs`: implements wide intermediates for checked decimal arithmetic.
