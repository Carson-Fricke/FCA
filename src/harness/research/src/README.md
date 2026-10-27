# src

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `acquisition.rs`: Owns option resolution, market acquisition and pinned replay input planning.
- `config.rs`: Building engine instances from harness configurations.
- `configuration.rs`: Owns loading and freezing current research documents and authored sources.
- `error.rs`: Owns typed failures crossing research configuration, acquisition and publication boundaries.
- `ledger.rs`: Owns fill-to-trade reconstruction and exact valuation/report projections.
- `lib.rs`: Owns replay/screening job composition, acquisition and artifact publication.
- `market_replay.rs`: Owns lake snapshot planning, bounded cursor threads and
  chronological merging through a fixed tournament over at most 32 cursor heads.
  Maps bound Arrow batches directly into replay events, moving identifiers through
  acquisition instead of retaining an intermediate normalized-event vector.
- `publishing.rs`: Owns current research artifact paths, Parquet/report writes and publication progress.
- `replay.rs`: Owns application composition from acquired inputs through replay and result publication.
- `run.rs`: Configuration-level run entry points over the JIT seam.
- `screen.rs`: One-shot screening execution over the JIT seam.
- `screen_run.rs`: Configuration-level screening runs over the JIT seam.
- `source.rs`: The screening data-source port and its market-lake adapter.

- `history.rs`: Owns replay scratch allocation and telemetry cadence at the application boundary.
