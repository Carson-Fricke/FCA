# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `config_parity.rs` — Configuration parity goldens.
- `document_syntax.rs` — Behavior checks for document syntax.
- `options.rs` — Strict option selectors survive the public submission configuration boundary.
