# Finite coordination process

- `lake-ingest-dispatcher.rs` composes durable job storage and the ACI adapter, performs one reconciliation pass, then exits. It never retrieves provider credentials or ingests market data.

The scheduled coordination job shares the dispatcher lease and implementation with the active API; it keeps queued work progressing when the API scales to zero.
