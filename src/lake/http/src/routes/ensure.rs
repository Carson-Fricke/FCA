//! Authenticated ensure and job polling routes.

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use lake_catalog::JobRecord;
use lake_contracts::{AssetClass, DateTime};
use lake_coverage::{ensure_coverage, EnsureOutcome, EnsureSpec, EnsureSubmission};

use crate::error::ApiError;
use crate::models::{
    parse_ensure_body, CatalogCoverageResponse, CoverageResponseBody, EnsureAcceptedResponse,
    EnsureBody, EquityEnsureRequest, JobStatusResponse, OptionEnsureRequest,
};
use crate::routes::catalog::option_response;
use crate::state::ApiContext;
use crate::validate::StrictJson;
use lake_coverage::{contracts_or_default, option_coverage, OptionRequestLike};

/// Return existing coverage or launch asynchronous ensure work.
pub(crate) async fn ensure(
    State(context): State<ApiContext>,
    StrictJson(value): StrictJson,
) -> Result<Response, ApiError> {
    let body = parse_ensure_body(&value)?;
    let as_of = context.coverage_as_of();
    match body {
        EnsureBody::Equity(request) => ensure_equity(&context, &request, as_of).await,
        EnsureBody::Option(request) => ensure_option(&context, &request, as_of).await,
    }
}

/// Return the current state of one persisted ensure job.
pub(crate) async fn job_status(
    State(context): State<ApiContext>,
    Path(job_id): Path<String>,
) -> Result<Json<JobStatusResponse>, ApiError> {
    if job_id.is_empty() || job_id.chars().count() > 128 {
        return Err(ApiError::validation_error(
            "job_id",
            "String should have at least 1 character and at most 128 characters",
        ));
    }
    let record = context.state.jobs.get(&job_id).map_err(ApiError::from)?;
    match record {
        Some(record) => Ok(Json(JobStatusResponse {
            job_id: record.job_id,
            state: record.state.value().to_string(),
            created_at: record.created_at,
            updated_at: record.updated_at,
            error_message: record.error_message,
            progress: record.progress,
        })),
        None => Err(ApiError::job_not_found()),
    }
}

async fn ensure_equity(
    context: &ApiContext,
    request: &EquityEnsureRequest,
    as_of: DateTime,
) -> Result<Response, ApiError> {
    let spec = EnsureSpec {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: request.symbol.clone(),
        start_date: request.start_date,
        end_date: request.end_date,
        interval: request.interval.clone(),
    };
    let jobs = Arc::clone(&context.state.jobs);
    let launcher = context
        .state
        .ensure_admission
        .clone()
        .ok_or_else(|| ApiError::internal("external ingestion is not configured"))?;
    let outcome = ensure_coverage(
        context.state.catalog.as_ref(),
        &jobs,
        context.state.listings.as_deref(),
        launcher,
        &spec,
        as_of,
        request.job_id.as_deref(),
    )
    .await
    .map_err(|error| ApiError::from_coverage(&error))?;
    match outcome {
        EnsureOutcome::Covered(report) => Ok(Json(CoverageResponseBody::Standard(
            CatalogCoverageResponse::from_report(&report, spec.data_type, &spec.symbol),
        ))
        .into_response()),
        EnsureOutcome::Accepted(record) => Ok(accepted_response(&record)),
    }
}

async fn ensure_option(
    context: &ApiContext,
    request: &OptionEnsureRequest,
    as_of: DateTime,
) -> Result<Response, ApiError> {
    let catalog = context.state.catalog.as_ref();
    let jobs = context.state.jobs.as_ref();
    let contracts = contracts_or_default(context.state.contracts.as_deref());
    let front_sets = context.state.front_sets.as_deref();
    let like = OptionRequestLike {
        data_type: request.data_type,
        interval: request.interval.as_deref(),
        symbol: request.symbol.as_deref(),
        underlying: request.underlying.as_deref(),
        contract_symbols: request.contract_symbols.as_deref(),
        start_date: request.start_date,
        end_date: request.end_date,
    };

    let (report, selection) = option_coverage(catalog, &like, Some(contracts), front_sets, as_of)
        .map_err(|error| ApiError::from_coverage(&error))?;
    if report.entries.iter().all(|entry| entry.is_terminal()) {
        return Ok(Json(CoverageResponseBody::Option(option_response(
            &report,
            request.data_type,
            &selection,
        )))
        .into_response());
    }

    let job_id = request
        .job_id
        .clone()
        .unwrap_or_else(|| lake_coverage::option_ensure_job_id(&like, &selection.symbols()));
    let launcher = context
        .state
        .ensure_admission
        .clone()
        .ok_or_else(|| ApiError::internal("external ingestion is not configured"))?;
    let spec = EnsureSpec {
        asset_class: AssetClass::Option,
        data_type: request.data_type,
        symbol: request.symbol.clone().unwrap_or_default(),
        start_date: request.start_date,
        end_date: request.end_date,
        interval: request.interval.clone(),
    };
    launcher(EnsureSubmission {
        job_id: job_id.clone(),
        requests: selection
            .symbols()
            .into_iter()
            .map(|symbol| EnsureSpec {
                symbol,
                ..spec.clone()
            })
            .collect(),
    })
    .await
    .map_err(ApiError::from)?;
    let accepted = jobs
        .get(&job_id)
        .map_err(ApiError::from)?
        .ok_or_else(|| ApiError::internal("ingestion admission did not retain its job ID"))?;
    if accepted.job_id != job_id {
        return Err(ApiError::internal("ingestion acknowledgement ID mismatch"));
    }
    Ok(accepted_response(&accepted))
}

/// Render the asynchronous acceptance with its `Location` header.
fn accepted_response(record: &JobRecord) -> Response {
    let body = EnsureAcceptedResponse {
        job_id: record.job_id.clone(),
        state: record.state.value().to_string(),
    };
    let mut response = (StatusCode::ACCEPTED, Json(body)).into_response();
    // Relative to catalog/ensure, preserving any host mount prefix.
    if let Ok(location) = HeaderValue::from_str(&format!("../jobs/{}", record.job_id)) {
        response.headers_mut().insert(header::LOCATION, location);
    }
    response
}
