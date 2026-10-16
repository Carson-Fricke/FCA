# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `bars.rs` — Regression coverage for provider-bar session grouping.
- `options_delta.rs` — Pure numerical model checks complement the HTTP option-selection behavior suite.

- `columnar.rs` — All-six tick-schema transaction round trips through the canonical writer and verified reader; exact decimals, unsigned IDs, nullable values, deterministic ordering, and fetch progress during publication.
