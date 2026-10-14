//! Resolve one individual contract before acquiring its ordinary datasets.
use crate::{error::ApiError, state::ApiContext};
use axum::{extract::State, http::StatusCode, Json};
use lake_contracts::{OptionSelector, ResolvedOption};

pub(crate) async fn resolve(
    State(context): State<ApiContext>,
    Json(value): Json<serde_json::Value>,
) -> Result<Json<ResolvedOption>, ApiError> {
    let selector: OptionSelector = serde_json::from_value(value)
        .map_err(|e| ApiError::validation_error("selector", e.to_string()))?;
    let as_of = selector
        .validate()
        .map_err(|e| ApiError::validation_error("selector", e.to_string()))?;
    if as_of > context.coverage_as_of() {
        return Err(ApiError::validation_error(
            "as_of",
            "selection cannot use a future timestamp",
        ));
    }
    let resolver = context.state.option_resolver.as_ref().ok_or_else(|| {
        ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "option_resolution_unavailable",
            "option resolution is not configured",
        )
    })?;
    Ok(Json(resolver(selector).await.map_err(|e| match e {
        lake_contracts::DataError::NotFound(message) => ApiError::new(
            StatusCode::NOT_FOUND,
            "option_selection_unavailable",
            message,
        ),
        lake_contracts::DataError::ValueError(message) => {
            ApiError::validation_error("selector", &message)
        }
        other => ApiError::new(
            StatusCode::BAD_GATEWAY,
            "option_resolution_failed",
            other.to_string(),
        ),
    })?))
}
