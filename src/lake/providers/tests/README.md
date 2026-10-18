# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `options.rs` — Provider protocol behavior for individual-option acquisition.
- `budget.rs` — Retries consume fleet permits and missing permits prevent transport dispatch.
- `transport.rs` — Real reqwest transport against loopback HTTP, covering concurrent responses, successful and empty bodies, malformed JSON, and rate-limit metadata.

- `decoding.rs` verifies all six tick-family exchanges, exact decimal tokens, nullable fields and malformed-page rejection through the real HTTP transport and provider adapter.
