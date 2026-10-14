# src

Alpaca transport, pacing, parsing, and acquisition. The option contract master includes active/inactive metadata; option quote/trade/bar requests share retry and governor policy. Pricing and persistence belong to ingestion and catalog.

- `client.rs` — Paginated Alpaca REST client over an injectable transport.
- `config.rs` — Alpaca endpoint, credential, retry, and budget settings.
- `error.rs` — Provider error taxonomy.
- `governor.rs` — Process-wide request-budget governor.
- `lane.rs` — Adaptive per-key request lane.
- `lib.rs` — Alpaca market-data client for the market lake.
- `parse.rs` — Alpaca response page and row parsing.
- `records.rs` — Normalized provider records independent of HTTP transport.
- `transport.rs` — Injectable HTTP transport and the production reqwest implementation.
