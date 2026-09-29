//! Authenticated ensure and job polling routes.

use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Path, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use lake_catalog::{JobRecord, JobState};
use lake_contracts::{AssetClass, DateTime};
use lake_coverage::{ensure_coverage, EnsureJob, EnsureJobLauncher, EnsureOutcome, EnsureSpec};
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::models::{
    parse_ensure_body, CatalogCoverageResponse, CoverageResponseBody, EnsureAcceptedResponse,
    EnsureBody, EquityEnsureRequest, JobStatusResponse, OptionEnsureRequest,
};
use crate::routes::catalog::option_response;
use crate::selection::{contracts_or_default, option_coverage, OptionRequestLike};
use crate::state::ApiContext;
use crate::validate::StrictJson;
use sha2::{Digest, Sha256};

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
        .ensure_launcher
        .clone()
        .unwrap_or_else(noop_launcher);
    let outcome = ensure_coverage(
        context.state.catalog.as_ref(),
        &jobs,
        context.state.listings.as_deref(),
        launcher,
        &spec,
        as_of,
        context.ensure_wait_deadline_seconds,
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
        symbol: request.symbol.as_deref(),
        underlying: request.underlying.as_deref(),
        contract_symbols: request.contract_symbols.as_deref(),
        contracts: request.contracts.as_deref(),
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

    let job_id = option_job_id(request, &selection.symbols());
    let existing = jobs.get(&job_id).map_err(ApiError::from)?;
    if let Some(record) = &existing {
        if matches!(record.state, JobState::Queued | JobState::Running) {
            return Ok(accepted_response(record));
        }
    }
    let created_at = existing.as_ref().map_or(as_of, |record| record.created_at);
    let accepted = JobRecord::new(job_id.clone(), JobState::Running, created_at, as_of, None)
        .map_err(ApiError::from)?;
    jobs.upsert(&accepted).map_err(ApiError::from)?;

    let launcher = context
        .state
        .ensure_launcher
        .clone()
        .unwrap_or_else(noop_launcher);
    let gaps = report.gap_plan().gaps;
    let mut symbols: Vec<String> = Vec::new();
    for gap in &gaps {
        if !symbols.contains(&gap.symbol) {
            symbols.push(gap.symbol.clone());
        }
    }
    let jobs_for_task = Arc::clone(&context.state.jobs);
    let spec = EnsureSpec {
        asset_class: AssetClass::Option,
        data_type: request.data_type,
        symbol: String::new(),
        start_date: request.start_date,
        end_date: request.end_date,
        interval: None,
    };
    let task_job_id = job_id.clone();
    let handle = tokio::spawn(async move {
        let mut failure = None;
        for symbol in &symbols {
            let job = EnsureJob {
                job_id: format!("{task_job_id}-{symbol}"),
                request: EnsureSpec {
                    symbol: symbol.clone(),
                    ..spec.clone()
                },
                gaps: gaps
                    .iter()
                    .filter(|gap| gap.symbol == *symbol)
                    .cloned()
                    .collect(),
            };
            if let Err(error) = launcher(job).await {
                failure = Some(error);
                break;
            }
        }
        let state = if failure.is_none() {
            JobState::Complete
        } else {
            JobState::Failed
        };
        let error_message = failure.as_ref().map(ToString::to_string);
        if let Ok(record) = JobRecord::new(task_job_id, state, created_at, as_of, error_message) {
            let _ = jobs_for_task.upsert(&record);
        }
    });

    let mut completed = false;
    let deadline =
        Duration::from_secs_f64(normalized_deadline(context.ensure_wait_deadline_seconds));
    if let Ok(Ok(())) = tokio::time::timeout(deadline, handle).await {
        completed = true;
    }
    if completed {
        let (refreshed, refreshed_selection) =
            option_coverage(catalog, &like, Some(contracts), front_sets, as_of)
                .map_err(|error| ApiError::from_coverage(&error))?;
        if refreshed.entries.iter().all(|entry| entry.is_terminal()) {
            return Ok(Json(CoverageResponseBody::Option(option_response(
                &refreshed,
                request.data_type,
                &refreshed_selection,
            )))
            .into_response());
        }
    }

    let current = jobs.get(&job_id).map_err(ApiError::from)?;
    Ok(accepted_response(&current.unwrap_or(accepted)))
}

/// Return the stable identifier for one option ensure membership.
fn option_job_id(request: &OptionEnsureRequest, symbols: &[String]) -> String {
    let mut payload = serde_json::Map::new();
    payload.insert("asset_class".to_string(), json!("option"));
    payload.insert("data_type".to_string(), json!(request.data_type.value()));
    payload.insert(
        "symbol".to_string(),
        json!(request
            .symbol
            .as_deref()
            .unwrap_or("")
            .trim()
            .to_uppercase()),
    );
    payload.insert(
        "underlying".to_string(),
        json!(request
            .underlying
            .as_deref()
            .unwrap_or("")
            .trim()
            .to_uppercase()),
    );
    payload.insert("contracts".to_string(), json!(symbols));
    payload.insert(
        "start_date".to_string(),
        json!(request.start_date.isoformat()),
    );
    payload.insert("end_date".to_string(), json!(request.end_date.isoformat()));
    let encoded = serde_json::to_string(&Value::Object(payload)).expect("plain JSON payload");
    format!(
        "ensure-{}",
        hex::encode(Sha256::digest(python_ascii_json(&encoded).as_bytes()))
    )
}

/// Escape non-ASCII characters exactly like Python's `json.dumps`.
fn python_ascii_json(text: &str) -> String {
    if text.is_ascii() {
        return text.to_string();
    }
    use std::fmt::Write as _;

    let mut escaped = String::with_capacity(text.len());
    let mut units = [0u16; 2];
    for character in text.chars() {
        if (character as u32) < 0x80 {
            escaped.push(character);
        } else {
            for unit in character.encode_utf16(&mut units) {
                let _ = write!(escaped, "\\u{unit:04x}");
            }
        }
    }
    escaped
}

/// Clamp a caller-supplied deadline to a usable, non-negative duration.
fn normalized_deadline(seconds: f64) -> f64 {
    if seconds.is_finite() && seconds > 0.0 {
        seconds
    } else {
        0.0
    }
}

/// The provider-free no-op launcher the Python boundary defaults to.
fn noop_launcher() -> EnsureJobLauncher {
    Arc::new(|_job| Box::pin(async { Ok(()) }))
}

/// Render the asynchronous acceptance with its `Location` header.
fn accepted_response(record: &JobRecord) -> Response {
    let body = EnsureAcceptedResponse {
        job_id: record.job_id.clone(),
        state: record.state.value().to_string(),
    };
    let mut response = (StatusCode::ACCEPTED, Json(body)).into_response();
    if let Ok(location) = HeaderValue::from_str(&format!("/v1/jobs/{}", record.job_id)) {
        response.headers_mut().insert(header::LOCATION, location);
    }
    response
}
