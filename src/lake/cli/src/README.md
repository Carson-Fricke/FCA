# Development-data cleanup command

- `lib.rs` parses `lake local reset --yes`, resolves the owned directory and delegates cleanup.
- `main.rs` is the thin binary entrypoint.

The harness API owns market-data HTTP hosting. ACI workers own all provider ingestion. No API or ingest command is implemented here.
