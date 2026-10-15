//! Publish checksum-pinned read manifests; signed locations are ephemeral transport metadata.

use crate::{
    error::ApiError,
    models::{ReadQuery, ReadQueryRaw},
    state::ApiContext,
    validate::StrictQuery,
};
use axum::{extract::State, Json};
use harness_market_reader::{MarketDataSnapshot, Partition, SnapshotRequest};
use std::collections::BTreeMap;

pub(crate) fn manifest(
    context: &ApiContext,
    query: &ReadQuery,
) -> Result<MarketDataSnapshot, ApiError> {
    let plan = plan(context, query, context.coverage_as_of())?;
    let request = SnapshotRequest {
        asset_class: query.asset_class.value().into(),
        data_type: query.data_type.value().into(),
        symbol: query.symbol.trim().to_uppercase(),
        start_date: query.start_date.isoformat(),
        end_date: query.end_date.isoformat(),
        interval: query.interval.clone(),
    };
    let files = plan
        .files
        .into_iter()
        .map(|file| {
            Ok(Partition {
                session_date: file.session_date.isoformat(),
                object_path: file.object_path,
                checksum: file.checksum.ok_or_else(|| {
                    ApiError::internal("partition metadata is outdated: checksum required")
                })?,
                row_count: file.row_count,
                byte_size: file.byte_size,
            })
        })
        .collect::<Result<Vec<_>, ApiError>>()?;
    MarketDataSnapshot::new(request, files).map_err(|e| ApiError::internal(e.to_string()))
}

pub(crate) async fn snapshot(
    State(context): State<ApiContext>,
    StrictQuery(raw): StrictQuery<ReadQueryRaw>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let query = ReadQuery::parse(raw)?;
    // Catalog and SAS adapters expose synchronous ports; do not block the shared host's reactor.
    tokio::task::spawn_blocking(move || {
        let snapshot = manifest(&context, &query)?;
        let locations = signed_locations(&context, &snapshot)?;
        Ok(Json(
            serde_json::json!({"snapshot":snapshot,"locations":locations}),
        ))
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))?
}

fn signed_locations(
    context: &ApiContext,
    snapshot: &MarketDataSnapshot,
) -> Result<BTreeMap<String, String>, ApiError> {
    let issuer = context
        .state
        .sas_issuer
        .as_ref()
        .ok_or_else(|| ApiError::internal("signed object reads are not configured"))?;
    snapshot
        .files
        .iter()
        .map(|file| {
            issuer
                .issue(&file.object_path)
                .map(|url| (file.checksum.clone(), url))
                .map_err(|e| ApiError::internal(e.to_string()))
        })
        .collect()
}

/// Renew access to the same pinned manifest; this never substitutes newer catalog contents.
pub(crate) async fn locations(
    State(context): State<ApiContext>,
    Json(snapshot): Json<MarketDataSnapshot>,
) -> Result<Json<BTreeMap<String, String>>, ApiError> {
    snapshot
        .validate()
        .map_err(|e| ApiError::validation_error("snapshot", e.to_string()))?;
    tokio::task::spawn_blocking(move || signed_locations(&context, &snapshot).map(Json))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))?
}

use lake_contracts::DateTime;
use lake_query::{FileQueryContext, ReadPlan, ReadRequest};
/// Plan snapshots through the same application policy used by native execution.
pub(crate) fn plan(
    context: &ApiContext,
    query: &ReadQuery,
    as_of: DateTime,
) -> Result<ReadPlan, ApiError> {
    lake_query::plan_snapshot(
        &FileQueryContext {
            catalog: context.state.catalog.as_ref(),
            listings: context.state.listings.as_deref(),
            sas_issuer: None,
            ensure_callback: None,
            as_of,
            bounded_query: None,
            force_authorized: false,
        },
        context.state.contracts.as_deref(),
        context.state.front_sets.as_deref(),
        &ReadRequest {
            asset_class: query.asset_class,
            data_type: query.data_type,
            symbol: query.symbol.clone(),
            interval: query.interval.clone(),
            start_date: query.start_date,
            end_date: query.end_date,
        },
    )
    .map_err(|e| ApiError::from_query(&e))
}
