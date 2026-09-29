//! Authenticated point-in-time reference routes.
//!
//! `GET /v1/reference/universe` returns the equity symbols with a listing in
//! the classification snapshot known at `as_of`; `GET /v1/reference/facts`
//! returns the point-in-time classification and fundamentals facts for the
//! requested symbols. Both read reference partitions through the injected
//! blob-read seam and reuse `lake-reference`'s selection rules, so the routes
//! never duplicate the as-of contract.

use axum::extract::State;
use axum::Json;
use lake_query::QueryApplicationError;

use crate::error::ApiError;
use crate::models::{
    parse_symbols_query, FactsQueryRaw, FactsResponse, UniverseQueryRaw, UniverseResponse,
};
use crate::state::ApiContext;
use crate::validate::{parse_date, StrictQuery};

/// Return the point-in-time equity universe known at `as_of`.
pub(crate) async fn universe(
    State(context): State<ApiContext>,
    StrictQuery(raw): StrictQuery<UniverseQueryRaw>,
) -> Result<Json<UniverseResponse>, ApiError> {
    let as_of = parse_date(&raw.as_of, "as_of")?;
    let store = context.read_store()?;
    let symbols = lake_query::universe_symbols(&*store, as_of).map_err(reference_error)?;
    Ok(Json(UniverseResponse { as_of, symbols }))
}

/// Return the point-in-time facts for the requested symbols.
pub(crate) async fn facts(
    State(context): State<ApiContext>,
    StrictQuery(raw): StrictQuery<FactsQueryRaw>,
) -> Result<Json<FactsResponse>, ApiError> {
    let as_of = parse_date(&raw.as_of, "as_of")?;
    let symbols = parse_symbols_query(&raw.symbols)?;
    let store = context.read_store()?;
    let facts = lake_query::symbol_facts(&*store, as_of, &symbols).map_err(reference_error)?;
    Ok(Json(FactsResponse { as_of, facts }))
}

/// Map a reference read failure onto the error envelope.
///
/// Reference partition failures are internal storage failures, not request
/// validation failures; everything else keeps the shared query mapping.
fn reference_error(error: QueryApplicationError) -> ApiError {
    if error.code == "internal_error" {
        ApiError::internal(error.message)
    } else {
        ApiError::from_query(&error)
    }
}
