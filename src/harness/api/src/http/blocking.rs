//! Bounded synchronous control admission, independent of asynchronous health.
use super::models::ApiError;
use axum::http::StatusCode;
use std::sync::Arc;
use tokio::sync::Semaphore;
pub async fn run<T: Send + 'static>(
    slots: Arc<Semaphore>,
    work: impl FnOnce() -> T + Send + 'static,
) -> Result<T, ApiError> {
    let permit = slots.clone().try_acquire_owned().map_err(|_| {
        ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "control_busy",
            "control capacity is occupied; reconcile retained job_id before retrying a submission",
        )
    })?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        work()
    })
    .await
    .map_err(|_| {
        ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "control_failed",
            "control worker terminated; submission outcome may be unknown",
        )
    })
}
