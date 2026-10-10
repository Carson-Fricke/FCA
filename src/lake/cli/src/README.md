# Lake process commands

`lib.rs` parses and composes the published `lake api` process and the local
API, ingest, and reset commands. Published API construction delegates to
`lake-hosted`, which selects Azure catalog, jobs, reads, and ingestion when
storage is configured. Local commands depend on `lake-local`; they do not
construct hosted adapters. `main.rs` is the thin binary entrypoint calling
`lake_cli::run`.

Behavior tests live in the sibling `tests/` directory. This folder must not
contain a separate API router implementation or in-source test modules.
