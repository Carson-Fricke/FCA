# Storage adapters

Provider-independent blob, Parquet, capacity and lease boundaries. Consumers use the canonical `BlobStore` and `ReadableBlobStore` ports.

- `lib.rs` exports the storage facade.
- `ports.rs` declares blob and SAS ports.
- `azure.rs` implements Azure storage adapters. Listings exclude hierarchical-storage directory entries, matching local file enumeration. Independent SDK operations execute concurrently; synchronous callers receive only their own operation's result. Manifest creation uses atomic `If-None-Match: *` uploads; final visibility PUTs carry the current lease ID on that same manifest blob. Finite leases expose their renewal cadence.
- `local.rs` implements local storage adapters.
- `parquet.rs` validates current Arrow batch schemas and partition symbols, then writes canonical ZSTD/Parquet 2.0 bytes under immutable SHA-256 paths with encoding/hash measurements. Typed record normalization belongs to ingestion; storage has no JSON row decoder.
- `capacity.rs` provides storage capacity accounting.
- `leases.rs` implements storage lease coordination.

Market-data decoding and caching belong to `harness-market-reader`; this crate owns canonical writing and storage adapters.

`azure.rs::container_from_account_url` exposes the same conditional Blob adapter
to durable harness execution; the application layer owns job claims and policy.
Application metadata supports bounded version reads and ETag-conditional PUTs,
so progress and heartbeat writers never wait behind an application metadata blob
lease. HTTP failures retain status and service code; object and market-publication
lease behavior remains at its existing boundary.

- `staged.rs` — Owned, sealed scratch files with byte admission, bounded hashing and automatic cleanup.

Local object replacement stages under a reserved `.fca-staging` directory on the same filesystem. That directory is outside the logical object namespace and cannot be addressed through storage ports; listings expose committed names only. Publication readers retain strict manifest-path validation.
