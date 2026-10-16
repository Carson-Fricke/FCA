# Market-data configuration

Strict configuration values shared by retained Python libraries and native
capabilities. Configuration does not construct providers, host services or
execute ingestion. ACA execution resources belong to `ingest-jobs` and deployment
inputs; retired queue-delivery settings are rejected.

- `__init__.py` exports the Python configuration facade.
- `loader.py` resolves configuration roots and loads strict YAML mappings.
- `settings.py` validates provider, storage, planning, API and operations settings.
- `universes.py` validates equity, crypto and option universe settings.
- `Cargo.toml` declares the corresponding Rust configuration crate.
- `src/` implements the native loaders and validated values.
