# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `bars.rs` — Range acquisition and per-session leased publication, partial publication failure without replay, and exact crypto midnight ownership in derived bars.
- `leased.rs` — Concurrent symbol-day publications through the real object/catalog committer, bounded busy acquisition, immediate non-conflict errors, and uncertain write outcomes without replay.
- `options_delta.rs` — Pure numerical model checks complement the HTTP option-selection behavior suite.

- `columnar.rs` — All-six tick-schema transaction round trips through the canonical writer and verified reader; exact decimals, unsigned IDs, nullable values, deterministic ordering, and fetch progress during publication.
