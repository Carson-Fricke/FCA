# Shared ingestion admission and measurements

- `lib.rs` defines validated environment limits, process composition, CPU/response/session/publication reservations, cancellation serialization and structured stage/pool snapshots. Reservations follow actual blocking work; oversized normalized datasets fail visibly without self-deadlock.
- `usage.rs` samples process CPU and RSS through native Windows/Unix APIs, keeping OS usage separate from allocation accounting.

Acquisition policy, codecs, storage adapters and durable job settlement belong to consumers. This capability has no provider credentials or external-service clients.
