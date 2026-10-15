# src

Authenticated market-lake routes and their injected application state. Individual option resolution precedes ordinary catalog/ensure/read/query acquisition. Provider implementations belong to composition crates.

- `auth.rs` — Bearer authentication for the market-lake HTTP boundary.
- `error.rs` — The error envelope shared by every market-lake HTTP response.
- `lib.rs` — Axum boundary publishing the market-lake catalog, ensure, query, read, raw,
- `models.rs` — Strict request and response models for the market-lake HTTP boundary.
- `state.rs` — Injected state and request-boundary configuration for the lake API.
- `validate.rs` — Serde-based extractors and the request validators shared by every route.
- `routes/` contains the corresponding implementation slice.

Explicit option lists use `contract_symbols`; the retired `contracts` request spelling is rejected. `symbol` remains a supported selector.
