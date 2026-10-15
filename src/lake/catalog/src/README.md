# Catalog implementation

Provider-independent catalog records and persistence ports, with in-memory, local JSON, and Azure Table adapters. Ingestion policy and HTTP composition belong to neighboring crates.

- `azure.rs` maps current records to Table entities and implements Azure repositories. Independent SDK operations execute concurrently; synchronous callers receive their own operation's result.
- `front_sets.rs` defines daily option front sets and their in-memory repository.
- `lib.rs` exports the public catalog facade.
- `memory.rs` implements in-memory catalog repositories.
- `models.rs` defines and validates persisted catalog records.
- `ports.rs` declares repository interfaces and coverage queries.
- `store.rs` implements the indexed local JSON catalog store.
