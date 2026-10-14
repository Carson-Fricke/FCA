# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `ensure.rs` — Public ensure behavior: bars never fetch ticks, quote/trade requests fetch only their dataset, and terminal sessions are skipped when extending a window.
- `options.rs` — Individual option resolution and all three datasets through the real local HTTP composition.
