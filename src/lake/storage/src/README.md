# Storage adapters

Provider-independent blob, Parquet, capacity, lease, queue, and streaming boundaries. Consumers use the canonical `BlobStore` and `ReadableBlobStore` ports; there is no parity alias.

- `lib.rs` exports the storage facade.
- `ports.rs` declares blob, SAS, queue, and lease ports.
- `azure.rs` implements Azure storage adapters. Independent SDK operations execute concurrently; synchronous callers receive only their own operation's result. Lock creation uses atomic `If-None-Match: *` uploads; an existing empty lock is leased without downloading or replacing it.
- `local.rs` implements local storage adapters.
- `parquet.rs` writes canonical market-data Parquet.
- `parquet_read.rs` reads canonical Parquet rows.
- `stream.rs` provides bounded Arrow batch streaming.
- `capacity.rs` provides storage capacity accounting.
- `leases.rs` implements storage lease coordination.
