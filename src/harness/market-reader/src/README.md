# Verified market-data reads

Sources supply compressed bytes. This folder never ensures coverage, contacts a provider, launches work or runs a program. Four active partition reads bound candidate decode memory separately from retained cache budgets. Bounded lookahead overlaps downloads and decoding while batches remain in snapshot order. At most three speculative reads leave capacity for required head reads across concurrent snapshots; visitor failure cancels scoped download futures. Compressed partitions above 64 MiB bypass speculative loading and remain ordered required head reads, avoiding several exceptionally large payloads in lookahead memory.

- `lib.rs` exports the intentional reader, source, snapshot, configuration and error contracts.
- `snapshot.rs` validates current SHA-256-prefixed partition paths and immutable manifests without signed URLs; outdated mutable paths are rejected.
- `reader.rs` coalesces preparation, validates checksums/schema/counts, prefetches a bounded partition window and visits ordered Arrow batches. Oversized partitions stream from a bounded decoded prefix without re-decoding it.

Pull-based replay cursors share these content caches but use one independent
decode permit and no speculative prefetch. Holding another cursor's head cannot
consume the permit needed to obtain the next source's chronological head.
- `cache.rs` owns the bounded decoded LRU and atomic checksum-addressed disk cache.
- `config.rs` resolves explicit cache budgets and directories.
- `error.rs` distinguishes manifest, source, integrity, cache and decode failures.
