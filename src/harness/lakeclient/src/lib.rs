//! Async HTTP client for the frozen market-lake read surface.
//!
//! One client serves the harness's lake needs over the published routes:
//!
//! * `POST /v1/catalog/ensure`, `GET /v1/jobs/{id}`, and
//!   `GET /v1/catalog/coverage` for window ensure and its poll loop;
//! * `GET /v1/snapshot` for pinned manifests and signed compressed objects,
//!   decoded by the harness-owned read-only reader;
//! * `GET /v1/reference/universe` and `GET /v1/reference/facts` for the
//!   point-in-time reference views.
//!
//! Configuration resolves from `LAKE_API_URL` (then `LAKE_API_BASE_URL`) and
//! `LAKE_API__BEARER_TOKEN`. The bearer token travels only in the
//! `Authorization` header; it is never placed in a URL, body, or log line, and
//! the debug rendering of the client redacts it.
//! Without a URL override, the client uses the Azure development lake.
//! Set `LAKE_API_URL` to select another combined harness market-data service.
//!
//! [`LakeClient::read_arrow`] returns no batches for an empty pinned manifest.
//! Every other lake failure is a typed [`LakeClientError`].
//!
//! [`BlockingLakeClient`] is the synchronous facade for callers that cannot
//! await (the JIT screening seam and the pre-run ensure paths).

mod access;
mod blocking;
pub use access::{
    Facts, MarketAccess, MarketAcquisition, MarketOptions, MarketReference, MarketSnapshots,
    OptionResolution,
};
mod client;
mod ensure;
mod error;
mod models;
mod snapshot;
mod spec;

#[cfg(feature = "test-support")]
pub mod testing;

pub use blocking::BlockingLakeClient;
pub use client::{LakeClient, LakeClientConfig, DEFAULT_LAKE_BASE_URL, MAX_PARALLEL_FETCHES};
pub use error::{EnsureFailure, LakeClientError};
pub use models::{CoverageEntry, CoverageReport, EnsureResponse, JobStatus};
pub use spec::{EnsureSpec, ReadSpec};

// The manifest is a value contract shared by planning and actual reads.
pub use harness_market_reader::MarketDataSnapshot;
