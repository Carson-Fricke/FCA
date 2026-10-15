//! Compose the authenticated market-data router over required Azure control/read adapters.

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::Router;
use lake_configuration::LakeSettings;
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

/// Build the market-data router; incomplete Azure configuration fails startup.
pub fn create_api_router(
    environ: &BTreeMap<String, String>,
    now: Arc<dyn Fn() -> lake_contracts::DateTime + Send + Sync>,
) -> Result<Router, HostedEnsureError> {
    Ok(create_host(environ, now, "http://127.0.0.1:8001/market-data")?.router)
}

pub struct MarketHost {
    pub router: Router,
    pub access: Arc<dyn harness_lakeclient::MarketAccess>,
}
pub fn create_host(
    environ: &BTreeMap<String, String>,
    now: Arc<dyn Fn() -> lake_contracts::DateTime + Send + Sync>,
    local_base: &str,
) -> Result<MarketHost, HostedEnsureError> {
    let settings = LakeSettings::load(None, environ)?;
    let token = bearer_token(environ, &settings)
        .ok_or_else(|| HostedEnsureError::new("LAKE_API__BEARER_TOKEN is required"))?;
    let (hosted, objects) = match environ
        .get("HARNESS_MARKET_DATA_MODE")
        .map(String::as_str)
        .unwrap_or("local")
    {
        "local" => crate::local::local_overrides(environ, local_base, &token)?,
        "azure" => (hosted_overrides(environ)?, Router::new()),
        _ => {
            return Err(HostedEnsureError::new(
                "HARNESS_MARKET_DATA_MODE must be local or azure",
            ))
        }
    };
    let access = Arc::new(crate::access::NativeMarketAccess {
        catalog: hosted.catalog.clone(),
        jobs: hosted.jobs.clone(),
        contracts: hosted.contracts.clone(),
        admission: hosted.job_admission.clone(),
        options: hosted.option_resolver.clone(),
        source: crate::access::StoredObjects(hosted.read_store.clone()),
        now: now.clone(),
    });

    let settings = LakeSettings::load(None, environ)?;
    let bearer_token = Some(token);

    let state = Arc::new(LakeApiState {
        catalog: hosted.catalog,
        jobs: hosted.jobs,
        listings: None,
        contracts: Some(hosted.contracts),
        front_sets: None,
        option_resolver: Some(hosted.option_resolver),
        sas_issuer: Some(hosted.sas_issuer),
        read_store: Some(hosted.read_store),
        ensure_admission: Some(hosted.job_admission),
        ensure_callback: Some(hosted.ensure_callback),
        bounded_query: None,
        authorized_force_override: settings.api.sql.authorized_force_override_default,
        bearer_token,
        entra: entra_authenticator(&settings)?,
        now,
    });
    Ok(MarketHost {
        router: create_app(state, LakeApiConfig { bearer_token: None }).merge(objects),
        access,
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
