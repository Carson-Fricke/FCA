# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `options.rs` — Provider protocol behavior for individual-option acquisition.
- `transport.rs` — Real reqwest transport against loopback HTTP, covering concurrent responses, successful and empty bodies, malformed JSON, and rate-limit metadata.
