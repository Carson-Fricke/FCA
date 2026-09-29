//! Async HTTP client for the frozen market-lake read surface.
//!
//! One client serves the harness's lake needs over the published routes:
//!
//! * `POST /v1/catalog/ensure`, `GET /v1/jobs/{id}`, and
//!   `GET /v1/catalog/coverage` for window ensure and its poll loop;
//! * `GET /v1/read` for streaming Arrow IPC dataset reads;
//! * `GET /v1/reference/universe` and `GET /v1/reference/facts` for the
//!   point-in-time reference views.
//!
//! Configuration resolves from `LAKE_API_URL` (then `LAKE_API_BASE_URL`) and
//! `LAKE_API__BEARER_TOKEN`. The bearer token travels only in the
//! `Authorization` header; it is never placed in a URL, body, or log line, and
//! the debug rendering of the client redacts it.
//!
//! [`LakeClient::read_arrow`] maps a `404 no_data` response to an empty batch
//! list: a window without ensured data is a normal empty read, not an error.
//! Every other lake failure is a typed [`LakeClientError`].
//!
//! [`BlockingLakeClient`] is the synchronous facade for callers that cannot
//! await (the JIT screening seam and the pre-run ensure paths).

mod blocking;
mod client;
mod ensure;
mod error;
mod models;
mod spec;

#[cfg(feature = "test-support")]
pub mod testing;

pub use blocking::BlockingLakeClient;
pub use client::{LakeClient, LakeClientConfig, DEFAULT_LAKE_BASE_URL};
pub use error::{EnsureFailure, LakeClientError};
pub use models::{CoverageEntry, CoverageReport, JobStatus};
pub use spec::{EnsureSpec, ReadSpec};
