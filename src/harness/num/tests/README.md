# Numeric behavior tests

These pure deterministic tests pin numeric contracts without storage or services.

- `decimal.rs`: variable-scale contexts, text, ordering and exact conversion.
- `golden.rs`: fixed-scale reference vectors and algebraic properties.
- `exact_ratios.rs`: independent wide-integer oracle for cancellation, operand
  sizes, all rounding modes, zero divisors and overflow boundaries.
- `golden_data/`: retained reference vectors used by `golden.rs`.
