# src

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `lib.rs`: In-memory and local file implementations of execution storage.

- `local.rs`: Owns marker-free local conditional objects, file streaming and atomic replacement.

- `memory.rs`: Owns in-memory conditional objects for isolated compositions and tests.
