//! Route modules and the shared unmatched-route fallbacks.

pub mod catalog;
pub mod ensure;
pub mod health;
pub mod options;
pub mod query;
pub mod raw;
pub mod reference;
pub mod snapshot;

use crate::error::ApiError;

/// The 404 envelope for an unmatched route, rendered without authentication.
pub(crate) async fn fallback() -> ApiError {
    ApiError::not_found()
}

/// The 405 envelope for a matched path with an unsupported method.
pub(crate) async fn method_not_allowed() -> ApiError {
    ApiError::method_not_allowed()
}
