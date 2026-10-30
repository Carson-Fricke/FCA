# src

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `lib.rs`: In-memory and local file implementations of execution storage.

- `local.rs`: Owns marker-free local conditional objects, file streaming, no-clobber creation and atomic replacement (Windows `ReplaceFileW` preserves open readers). Publication failures identify the destination and retain the OS error; uncertain Windows partial replacements retain staged bytes for reconciliation without retries.

- `memory.rs`: Owns in-memory conditional objects for isolated compositions and tests.
