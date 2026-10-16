# Storage adapters

Provider-independent blob, Parquet, capacity and lease boundaries. Consumers use the canonical `BlobStore` and `ReadableBlobStore` ports.

- `lib.rs` exports the storage facade.
- `ports.rs` declares blob and SAS ports.
- `azure.rs` implements Azure storage adapters. Independent SDK operations execute concurrently; synchronous callers receive only their own operation's result. Lock creation uses atomic `If-None-Match: *` uploads; an existing empty lock is leased without downloading or replacing it.
- `local.rs` implements local storage adapters.
- `parquet.rs` validates current Arrow batch schemas and partition symbols, then writes canonical ZSTD/Parquet 2.0 bytes with exact SHA256 metadata. Typed record normalization belongs to ingestion; storage has no JSON row decoder.
- `capacity.rs` provides storage capacity accounting.
- `leases.rs` implements storage lease coordination.

Market-data decoding and caching belong to `harness-market-reader`; this crate owns canonical writing and storage adapters.
