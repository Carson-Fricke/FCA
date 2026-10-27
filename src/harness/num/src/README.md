# Exact numeric primitives

This folder contains pure checked numeric kernels; it has no storage, transport,
job or provider dependencies. Decimal conversion and rounding belong here rather
than passing through text in replay or reporting.

- `lib.rs`: exports the numeric facade.
- `decimal.rs`: implements variable-scale decimal arithmetic and float boundaries;
  exact narrow results bypass digit-vector construction without changing scale.
- `decimal18.rs`: implements checked fixed-scale arithmetic, exact factor
  cancellation, narrow product/ratio paths and direct variable-scale conversion,
  preserving rounding and overflow rules.
- `wide.rs`: implements wide intermediates, using narrow division where possible
  and visiting only possible quotient bits in wide division.
