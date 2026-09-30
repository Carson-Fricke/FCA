//! Local composition over filesystem state: layout, ingest, backfill, and the ensure launcher.
//!
//! This crate is the Rust port of `lake/local`: the deterministic local
//! directory tree with its ownership marker, the Alpaca ingest and backfill
//! pipelines over the local filesystem adapters, the on-demand ensure launcher,
//! and the composition that serves the published HTTP contract from local
//! state. Credentials, settings, and provider boundaries are injected so the
//! whole surface is exercised offline with fakes.
//!
//! Three semantics are load-bearing and reproduced exactly:
//!
//! 1. **Marker ownership** — initialization refuses a non-empty directory not
//!    created by local mode, and reset removes only a tree whose marker carries
//!    the pinned content.
//! 2. **Resume from the catalog** — a backfill skips sessions that are terminal
//!    for every requested data type, so a repeated ensure converges instead of
//!    refetching.
//! 3. **Bounded, repairing walks** — sessions run concurrently within a fixed
//!    budget; per-session failures surface as error coverage and one settle
//!    delay later the transient ones get a second attempt in the same run.

mod application;
mod committer;
mod ensure;
mod ingest;
mod state;

pub use application::{create_local_app, create_local_app_with, LocalProviders};
pub use committer::LocalIngestCommitter;
pub use ensure::{
    local_ensure_deadline_seconds, LocalEnsureLauncher, DEFAULT_ENSURE_DEADLINE_SECONDS,
};
pub use ingest::{
    run_local_backfill, run_local_bars_backfill, run_local_crypto_backfill, run_local_ingest,
    BackfillProgress, BackfillProgressSink, BackfillResult, CryptoBackfillResult,
    IngestCoverageEntry, IngestObjectEntry, IngestResult, LocalBackfillOptions,
    LocalBarsBackfillOptions, LocalCryptoBackfillOptions, LocalIngestError, LocalIngestOptions,
    SessionIngestEntry,
};
pub use state::{
    LocalCatalog, LocalFileIssuer, LocalLayout, LocalStateError, MARKER_CONTENT, MARKER_NAME,
};
