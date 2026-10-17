# src

Alpaca transport, pacing, parsing, and acquisition. The option contract master includes active/inactive metadata; option quote/trade/bar requests share retry and governor policy. Pricing and persistence belong to ingestion and catalog.

- `client.rs` — Paginated Alpaca REST client over an injectable transport.
- `config.rs` — Alpaca endpoint, credential, retry, and budget settings.
- `error.rs` — Provider error taxonomy.
- `governor.rs` — Request pacing and the injected fleet-budget admission port, acquired for every transport attempt.
- `lane.rs` — Adaptive per-key request lane.
- `lib.rs` — Alpaca market-data client for the market lake.
- `parse.rs` — Alpaca response page and row parsing.
- `records.rs` — Normalized provider records independent of HTTP transport.
- `transport.rs` — Injectable HTTP transport and the production reqwest implementation; response JSON decoding runs on the blocking pool so in-flight downloads retain async worker capacity.

Provider decoding and normalization share `lake-pipeline` CPU slots with ingestion encoding. Response headroom is reserved before request allowance and follows body ownership through blocking decode/normalization. Normalized pages are accounted before retention; oversized datasets fail visibly. Storage publication is outside this capability.
