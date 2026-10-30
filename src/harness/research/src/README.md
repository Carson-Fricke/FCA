# src

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `acquisition.rs`: Owns option resolution, market acquisition and pinned replay input planning.
- `config.rs`: Builds engine instances and projects internal decimal wrappers to
  exact JSON numbers at the authored parameter boundary.
- `configuration.rs`: Owns loading and freezing current research documents and authored sources.
- `error.rs`: Owns typed failures crossing research configuration, acquisition and publication boundaries.
- `ledger.rs`: Adapts booked fills to the shared trading FIFO matcher and owns
  exact valuation/report projections, trade-return units and capital-use summaries.
- `lib.rs`: Owns replay/screening job composition, acquisition and artifact publication.
- `market_replay.rs`: Owns lake snapshot planning, bounded cursor threads and
  chronological merging through a fixed tournament over at most 32 cursor heads.
  Prepares bounded tick blocks on cold reads and reuses complete prepared streams
  on warm reads. Bars remain owned source records for frame assembly.
- `prepared_replay.rs`: Owns content-addressed, checksummed block storage with a
  configurable disk budget. Keys bind the pinned source inventory, symbol alias,
  exact window and decoder sources. Only complete verified streams publish;
  interrupted writers cannot publish partial results. Cold writer contention
  bypasses retention to avoid cross-input deadlocks. Oldest complete entries are
  evicted at admission; active readers retain their opened immutable files.
- `publishing.rs`: Owns current research artifact paths, Parquet/report writes and publication progress.
- `replay.rs`: Owns application composition from acquired inputs through replay and result publication.
- `run.rs`: Configuration-level run entry points over the JIT seam.
- `screen.rs`: One-shot screening execution over the JIT seam.
- `screen_run.rs`: Configuration-level screening runs over the JIT seam.
- `source.rs`: The screening data-source port and its market-lake adapter.

- `history.rs`: Owns replay scratch allocation and telemetry cadence at the application boundary.
