//! The synchronous facade over the async lake client.
//!
//! The JIT screening seam and the pre-run ensure paths are synchronous; they
//! block on the async client through one dedicated runtime. When a caller is
//! already inside a Tokio runtime, the bridge runs the future on a scoped
//! thread so it never blocks the caller's executor thread.

use std::fmt;
use std::future::Future;

use arrow_array::RecordBatch;
use tokio::runtime::{Builder, Runtime};

use crate::client::{LakeClient, LakeClientConfig};
use crate::error::LakeClientError;
use crate::spec::{EnsureSpec, ReadSpec};

/// A blocking client over the frozen market-lake routes.
pub struct BlockingLakeClient {
    client: LakeClient,
    runtime: Option<Runtime>,
}

impl fmt::Debug for BlockingLakeClient {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("BlockingLakeClient")
            .field("client", &self.client)
            .finish()
    }
}

impl BlockingLakeClient {
    pub fn progress_reporter(&self) -> Option<harness_contracts::progress::ProgressReporter> {
        self.client.progress_reporter()
    }

    /// Reuse pinned immutable manifests and transports for a pull replay cursor.
    /// Cursor telemetry is owned by the replay, not by repeated data reads.
    pub fn replay_cursor(&self) -> Result<Self, LakeClientError> {
        Ok(Self {
            client: self.client.replay_cursor(),
            runtime: Some(
                Builder::new_multi_thread()
                    .worker_threads(1)
                    .enable_all()
                    .build()
                    .map_err(|error| LakeClientError::Config(error.to_string()))?,
            ),
        })
    }
    pub fn resolve_option(
        &self,
        selector: &harness_contracts::OptionSelector,
    ) -> Result<harness_contracts::ResolvedOption, LakeClientError> {
        self.block_on(self.client.resolve_option(selector))
    }
    pub fn from_access(
        access: std::sync::Arc<dyn crate::MarketAccess>,
    ) -> Result<Self, LakeClientError> {
        let runtime = Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .map_err(|e| LakeClientError::Config(e.to_string()))?;
        Ok(Self {
            client: LakeClient::from_access(access)?,
            runtime: Some(runtime),
        })
    }
    /// Build a blocking client from resolved configuration.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the client or its bridge runtime cannot be
    /// built.
    pub fn new(config: LakeClientConfig) -> Result<Self, LakeClientError> {
        let client = LakeClient::new(config)?;
        let runtime = Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .map_err(|error| {
                LakeClientError::Config(format!(
                    "the market-lake bridge runtime could not be built: {error}"
                ))
            })?;
        Ok(Self {
            client,
            runtime: Some(runtime),
        })
    }

    /// Build a blocking client from `LAKE_API_URL`/`LAKE_API_BASE_URL` and
    /// `LAKE_API__BEARER_TOKEN`.
    ///
    /// # Errors
    ///
    /// Returns [`LakeClientError::Config`] when the environment has no bearer
    /// token.
    pub fn from_env() -> Result<Self, LakeClientError> {
        Self::new(LakeClientConfig::from_env()?)
    }

    /// Create a bridge sharing the HTTP transports with a job-scoped telemetry port.
    pub fn reporting(
        &self,
        reporter: harness_contracts::progress::ProgressReporter,
    ) -> Result<Self, LakeClientError> {
        let runtime = Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .map_err(|error| LakeClientError::Config(error.to_string()))?;
        Ok(Self {
            client: self.client.clone().with_progress(reporter),
            runtime: Some(runtime),
        })
    }

    /// Attach the execution owner's telemetry port.
    pub fn with_progress(
        mut self,
        reporter: harness_contracts::progress::ProgressReporter,
    ) -> Self {
        self.client = self.client.clone().with_progress(reporter);
        self
    }

    pub fn report_progress(
        &self,
        progress: harness_contracts::progress::ExecutionProgress,
    ) -> Result<(), LakeClientError> {
        self.client.report_progress(progress)
    }

    /// Consume decoded batches without retaining the response or all Arrow batches.
    pub fn visit_arrow<F>(&self, spec: &ReadSpec, consume: F) -> Result<(), LakeClientError>
    where
        F: FnMut(RecordBatch) -> Result<(), LakeClientError> + Send,
    {
        self.block_on(self.client.visit_arrow(spec, consume))
    }

    /// The resolved base URL with any trailing slash removed.
    #[must_use]
    pub fn base_url(&self) -> &str {
        self.client.base_url()
    }

    /// Ensure one symbol/data-type window, polling to completion.
    ///
    /// # Errors
    ///
    /// Returns a typed error under the same conditions as
    /// [`LakeClient::ensure`].
    pub fn ensure(&self, spec: &EnsureSpec) -> Result<(), LakeClientError> {
        self.block_on(self.client.ensure(spec))
    }

    /// Read the ensured dataset window as verified Arrow batches.
    ///
    /// # Errors
    ///
    /// Returns a typed error under the same conditions as
    /// [`LakeClient::read_arrow`].
    pub fn read_arrow(&self, spec: &ReadSpec) -> Result<Vec<RecordBatch>, LakeClientError> {
        self.block_on(self.client.read_arrow(spec))
    }

    /// Ensure every spec concurrently, polling each to completion.
    ///
    /// The results keep the spec order; every spec is attempted even when
    /// another fails.
    pub fn ensure_many(&self, specs: &[EnsureSpec]) -> Vec<Result<(), LakeClientError>> {
        self.block_on(self.client.ensure_many(specs))
    }

    /// Read every spec concurrently, preserving the spec order.
    pub fn read_many(&self, specs: &[ReadSpec]) -> Vec<Result<Vec<RecordBatch>, LakeClientError>> {
        self.block_on(self.client.read_many(specs))
    }

    /// Return the as-of universe symbols.
    ///
    /// # Errors
    ///
    /// Returns a typed error under the same conditions as
    /// [`LakeClient::reference_universe`].
    pub fn reference_universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError> {
        self.block_on(self.client.reference_universe(as_of))
    }

    /// Return the engine fact-sheet entries per symbol as of `as_of`.
    ///
    /// # Errors
    ///
    /// Returns a typed error under the same conditions as
    /// [`LakeClient::reference_facts`].
    pub fn reference_facts(
        &self,
        symbols: &[String],
        as_of: &str,
    ) -> Result<
        std::collections::BTreeMap<String, std::collections::BTreeMap<String, String>>,
        LakeClientError,
    > {
        self.block_on(self.client.reference_facts(symbols, as_of))
    }

    /// Return the earliest fully covered session date for one window.
    ///
    /// # Errors
    ///
    /// Returns a typed error under the same conditions as
    /// [`LakeClient::earliest_complete_session`].
    pub fn earliest_complete_session(
        &self,
        spec: &EnsureSpec,
    ) -> Result<Option<String>, LakeClientError> {
        self.block_on(self.client.earliest_complete_session(spec))
    }

    /// Drive one future to completion without stalling a caller's executor.
    fn block_on<F>(&self, future: F) -> F::Output
    where
        F: Future + Send,
        F::Output: Send,
    {
        let runtime = self
            .runtime
            .as_ref()
            .expect("the market-lake bridge runtime is available");
        if tokio::runtime::Handle::try_current().is_ok() {
            std::thread::scope(|scope| {
                scope
                    .spawn(|| runtime.block_on(future))
                    .join()
                    .expect("the market-lake bridge thread panicked")
            })
        } else {
            runtime.block_on(future)
        }
    }
}

impl Drop for BlockingLakeClient {
    fn drop(&mut self) {
        // `shutdown_background` is safe inside an asynchronous context, where a
        // plain runtime drop would panic; the client is being discarded, so its
        // bridge tasks are already complete.
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_background();
        }
    }
}
