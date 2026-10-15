# Verified market-data reads

Sources supply compressed bytes. This folder never ensures coverage, contacts a provider, launches work or runs a program. Four active partition reads bound candidate decode memory separately from retained cache budgets.

- `lib.rs` exports the intentional reader, source, snapshot, configuration and error contracts.
- `snapshot.rs` validates canonical requests/partitions and hashes immutable manifests without signed URLs.
- `reader.rs` coalesces reads, validates checksums/schema/counts and visits bounded Arrow batches.
- `cache.rs` owns the bounded decoded LRU and atomic checksum-addressed disk cache.
- `config.rs` resolves explicit cache budgets and directories.
- `error.rs` distinguishes manifest, source, integrity, cache and decode failures.
