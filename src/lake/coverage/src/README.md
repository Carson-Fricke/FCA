# src

Session coverage and ensure planning over catalog ports. Options and equities share XNYS sessions; option bars retain their interval in coverage identity. This slice does not fetch market data.

- `application.rs` — Provider-independent coverage and ensure use cases.
- `error.rs` — The coverage application rejection type.
- `lib.rs` — Coverage classification, gap planning, and the ensure application.
- `service.rs` — Calendar-aware coverage and gap planning with dataset/symbol/interval catalog queries and trade revision validation for derived bars.
