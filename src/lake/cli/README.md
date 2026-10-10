# Market-lake entrypoint

`Cargo.toml` defines the native `lake-cli` package and its `lake` binary.
`__init__.py` publishes the retained Python console-script facade.
`rust_entrypoints.py` resolves and launches the Rust binary without a Python
service fallback.

`src/` owns command parsing and thin process composition. Published API
commands delegate to `lake-hosted`; local commands delegate to `lake-local`.
`tests/` verifies CLI composition behavior without real provider calls.
This package must not implement catalog, storage, ingestion, or HTTP routes.
