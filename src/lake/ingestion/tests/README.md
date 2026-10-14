# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `bars.rs` — Regression coverage for provider-bar session grouping.
- `options_delta.rs` — Pure numerical model checks complement the HTTP option-selection behavior suite.
