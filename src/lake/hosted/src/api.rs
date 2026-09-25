//! The published API process composition.
//!
//! Mirrors `lake.hosted.runtime.hosted_overrides` plus the Python
//! `lake.app.create_app` wiring: with `LAKE_STORAGE_ACCOUNT_NAME` present the
//! router carries the hosted Azure boundaries; without it, the in-memory
//! composition is used instead.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Router;
use lake_catalog::{
    MemoryCatalogRepository, MemoryContractRepository, MemoryDailyFrontSetRepository,
    MemoryJobRepository,
};
use lake_configuration::LakeSettings;
use lake_contracts::DateTime;
use lake_http::{create_app, LakeApiConfig, LakeApiState};

use crate::runtime::{hosted_overrides, HostedEnsureError};

const BEARER_TOKEN_ENV: &str = "LAKE_API__BEARER_TOKEN";

/// Resolve the static bearer token from the environment or settings.
fn bearer_token(environ: &BTreeMap<String, String>, settings: &LakeSettings) -> Option<String> {
    let from_environment = environ
        .get(BEARER_TOKEN_ENV)
        .map(String::as_str)
        .map(str::trim)
        .filter(|token| !token.is_empty())
        .map(str::to_string);
    from_environment.or_else(|| {
        settings
            .api
            .bearer_token
            .clone()
            .filter(|token| !token.trim().is_empty())
    })
}

/// Build the published API router, selecting the Azure composition when
/// `LAKE_STORAGE_ACCOUNT_NAME` is present and the in-memory one otherwise.
pub fn create_api_router(
    environ: &BTreeMap<String, String>,
    now: Arc<dyn Fn() -> lake_contracts::DateTime + Send + Sync>,
) -> Result<Router, HostedEnsureError> {
    let overrides = hosted_overrides(environ, None)?;
    let settings = LakeSettings::load(None, environ)?;
    let deadline = f64::from(settings.api.ensure_wait_deadline_seconds);
    let bearer_token = bearer_token(environ, &settings);

    let entra = entra_authenticator(&settings)?;
    let state = match overrides {
        Some(hosted) => Arc::new(LakeApiState {
            catalog: hosted.catalog,
            jobs: hosted.jobs,
            listings: None,
            contracts: None,
            front_sets: None,
            sas_issuer: Some(hosted.sas_issuer),
            ensure_launcher: Some(hosted.job_launcher),
            ensure_callback: Some(hosted.ensure_callback),
            bounded_query: None,
            authorized_force_override: settings.api.sql.authorized_force_override_default,
            ensure_wait_deadline_seconds: deadline,
            bearer_token,
            entra: entra_authenticator(&settings)?,
            now,
        }),
        None => Arc::new(in_memory_state(settings, bearer_token, entra, now)?),
    };
    Ok(create_app(
        state,
        LakeApiConfig {
            bearer_token: None,
            ensure_wait_deadline_seconds: deadline,
        },
    ))
}

/// The in-memory fallback state, mirroring the bare Python `create_app`.
fn in_memory_state(
    settings: LakeSettings,
    bearer_token: Option<String>,
    entra: Option<Arc<lake_http::auth::EntraJwtAuthenticator>>,
    now: Arc<dyn Fn() -> DateTime + Send + Sync>,
) -> Result<LakeApiState, HostedEnsureError> {
    let deadline = f64::from(settings.api.ensure_wait_deadline_seconds);
    Ok(LakeApiState {
        catalog: Arc::new(MemoryCatalogRepository::new()),
        jobs: Arc::new(MemoryJobRepository::new()),
        listings: None,
        contracts: Some(Arc::new(MemoryContractRepository::new())),
        front_sets: Some(Arc::new(MemoryDailyFrontSetRepository::new())),
        sas_issuer: None,
        ensure_launcher: None,
        ensure_callback: None,
        bounded_query: None,
        authorized_force_override: settings.api.sql.authorized_force_override_default,
        ensure_wait_deadline_seconds: deadline,
        bearer_token,
        entra,
        now,
    })
}

/// Build the Entra validator when both tenant and audience are configured.
fn entra_authenticator(
    settings: &LakeSettings,
) -> Result<Option<Arc<lake_http::auth::EntraJwtAuthenticator>>, HostedEnsureError> {
    let tenant = settings
        .api
        .auth
        .entra_tenant_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let audience = settings
        .api
        .auth
        .entra_audience
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    match (tenant, audience) {
        (Some(tenant), Some(audience)) => Ok(Some(Arc::new(
            lake_http::auth::EntraJwtAuthenticator::with_role(
                tenant,
                audience,
                &settings.api.auth.entra_role,
            )
            .map_err(|error| HostedEnsureError::new(error.to_string()))?,
        ))),
        _ => Ok(None),
    }
}
