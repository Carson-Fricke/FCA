# Catalog implementation

Provider-independent catalog records and persistence ports, with a shared leased-manifest coverage adapter, in-memory repositories, and Azure Table adapters for other metadata. Ingestion policy and HTTP composition belong to neighboring crates.

- `azure.rs` maps current records to Table entities and implements Azure repositories. Independent SDK operations execute concurrently; synchronous callers receive their own operation's result.
- `front_sets.rs` defines daily option front sets and their in-memory repository.
- `lib.rs` exports the public catalog facade.
- `memory.rs` implements in-memory catalog repositories.
- `models.rs` defines and validates persisted catalog records.
- `ports.rs` declares repository interfaces and coverage queries.
- `publications.rs` owns symbol-day manifest validation, atomic merged visibility, renewable ownership and current coverage reads through storage ports.
