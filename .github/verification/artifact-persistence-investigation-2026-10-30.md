# Windows local persistence investigation

This investigation proposes a fix; production persistence behavior was not changed.

## Recorded failures

- `job-179cf1f9b232447b9a189c61ebbc597c` failed during replay after 55,771,136
  records, with zero published artifacts. Its last replay progress update preceded
  failure by about one second. This is not exclusively an artifact-copy problem.
- `job-08a268e90fcd49de8e937368b82e03d5` finished replay and result computation,
  then failed during publication with five artifacts retained.
- Both report `failed to persist temporary file: Access is denied. (os error 5)`.

`execution-storage/src/local.rs` uses `NamedTempFile.persist` for replacement of
mutable job documents as well as initial artifact publication. Errors currently
discard the logical object key and operation. Per-object writer locks do not
exclude readers. Ordinary status/progress reads can overlap document replacement.

## Reproduction

A standalone ignored Rust probe used the same tempfile dependency on this Windows
machine. Holding an ordinary `std::fs::File::open` reader on the destination made
`NamedTempFile.persist` fail with the exact recorded error and OS code 5. Explicit
read/write/delete sharing did not prevent the failure. Releasing the reader and
persisting the same retained temporary file succeeded. With the ordinary reader
still held, `ReplaceFileW` succeeded.

This confirms a concrete replacement-versus-reader failure mechanism. The historical
errors lack destination context, so they do not prove which specific reader held
each failed destination. It is unnecessary to assume antivirus interference or
Git lock contention to reproduce the symptom.

## Proposed change

1. Put atomic local replacement in one storage-owned helper. On Windows, replace
   an existing destination with `ReplaceFileW`; use a no-clobber create path for
   new objects. Preserve existing per-object locks, fencing, temporary-file flushes
   and idempotent immutable-artifact admission. Unix retains rename semantics.
2. Preserve the temporary file on replacement failure. Return operation, logical
   object key, job identity when available, and OS error. Do not rerun an execution
   to repair publication or replace a terminal result with another execution.
3. Verify through the real local backend: replacement while an ordinary reader
   remains open, readers observing complete old/new bytes, competing publishers,
   permission denial, and immutable artifact conflict handling. Cross-platform
   atomicity and crash behavior need verification before adopting the primitive.

Blind retries for every access-denied error are not the proposed root fix. Genuine
permission failures must remain visible. Any additional transient-sharing retry
should be bounded, storage-local and retain the same temporary bytes.
