//! Ensure and job-polling route behavior.

mod support;

use std::sync::Arc;

use axum::http::StatusCode;
use lake_catalog::{
    CoverageRecord, CoverageStatus, JobRecord, JobState, MemoryCatalogRepository,
    MemoryContractRepository, MemoryJobRepository,
};
use lake_contracts::{AssetClass, DataType, Date};
use serde_json::json;
use support::*;

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn equity_body(start_date: &str, end_date: &str) -> serde_json::Value {
    json!({
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "AAPL",
        "start_date": start_date,
        "end_date": end_date,
    })
}

fn equity_record(session_date: Date, status: CoverageStatus) -> CoverageRecord {
    status_record(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        status,
    )
}

#[tokio::test]
async fn satisfied_ensure_returns_coverage_without_launching() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([
        equity_record(date(1, 18), CoverageStatus::Complete),
        equity_record(date(1, 19), CoverageStatus::Complete),
    ]));
    let launcher = RecordingAdmission::default();
    let mut state = base_state(catalog);
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let router = build_app(state);

    let (status, headers, body) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2024-01-18", "2024-01-19"),
        ),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["entries"]
            .as_array()
            .expect("entries")
            .iter()
            .map(|entry| entry["status"].clone())
            .collect::<Vec<_>>(),
        vec![json!("complete"), json!("complete")]
    );
    assert!(headers.get("location").is_none());
    assert!(launcher.jobs().is_empty());
}

#[tokio::test]
async fn poll_returns_not_found_with_the_consistent_error_shape() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(&router, get_auth("/v1/jobs/missing-job")).await;

    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        body,
        json!({"error": {"code": "job_not_found", "message": "the requested job does not exist"}})
    );
}

#[tokio::test]
async fn poll_exposes_injected_terminal_job_state() {
    let jobs = MemoryJobRepository::from_records([JobRecord::new(
        "ensure-existing",
        JobState::Complete,
        as_of(),
        as_of(),
        None,
    )
    .expect("valid job")]);
    let mut state = base_state(Arc::new(MemoryCatalogRepository::new()));
    state.jobs = Arc::new(jobs);
    let router = build_app(state);

    let (status, _, body) = send(&router, get_auth("/v1/jobs/ensure-existing")).await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["state"], "complete");
    assert_eq!(body["created_at"], "2024-01-22T12:00:00Z");
}

#[tokio::test]
async fn ensure_and_poll_require_authentication() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (ensure_status, _, _) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            None,
            equity_body("2024-01-18", "2024-01-19"),
        ),
    )
    .await;
    let (poll_status, _, _) = send(&router, get("/v1/jobs/unknown")).await;

    assert_eq!(ensure_status, StatusCode::UNAUTHORIZED);
    assert_eq!(poll_status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn ensure_accepts_a_range_beyond_the_former_catalog_cap() {
    let catalog = shared_catalog([]);
    let launcher = RecordingAdmission::default();
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2022-01-03", "2024-01-19"),
        ),
    )
    .await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    let launched = launcher.jobs();
    assert_eq!(launched.len(), 1);
    assert_eq!(launched[0].requests.len(), 1);
    assert_eq!(
        launched[0].requests[0].start_date,
        Date::from_ymd(2022, 1, 3).unwrap()
    );
    assert_eq!(
        launched[0].requests[0].end_date,
        Date::from_ymd(2024, 1, 19).unwrap()
    );
}

#[tokio::test]
async fn satisfied_option_ensure_returns_coverage_without_launching() {
    let contract_symbol = "SPY240119C00500000";
    let catalog = Arc::new(MemoryCatalogRepository::from_records([complete_record(
        AssetClass::Option,
        DataType::Trade,
        contract_symbol,
        date(1, 19),
    )]));
    let launcher = RecordingAdmission::default();
    let mut state = base_state(catalog);
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract(contract_symbol, "SPY", date(1, 19)),
    ])));
    let router = build_app(state);

    let body = json!({
        "asset_class": "option",
        "data_type": "trade",
        "contract_symbols": [contract_symbol],
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
    });
    let (status, _, response) =
        send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(response["entries"][0]["status"], "complete");
    assert!(launcher.jobs().is_empty());
}

#[tokio::test]
async fn ensure_rejects_unsupported_requests() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let unsupported = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            json!({
                "asset_class": "option",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-01-01",
            }),
        ),
    )
    .await;
    let unknown = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            json!({
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-01-01",
                "unexpected": "value",
            }),
        ),
    )
    .await;

    assert_eq!(unsupported.0, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(unsupported.2["error"]["code"], "unsupported_asset_class");
    assert_eq!(unknown.0, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(unknown.2["error"]["code"], "validation_error");
}

#[tokio::test]
async fn bars_ensure_requires_the_interval_and_keeps_job_ids_distinct() {
    let catalog = shared_catalog([]);
    let launcher = RecordingAdmission::default();
    let mut state = base_state(catalog);
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let router = build_app(state);

    let body = |interval: &str| {
        json!({
            "asset_class": "equity",
            "data_type": "bars",
            "symbol": "AAPL",
            "interval": interval,
            "start_date": "2024-01-18",
            "end_date": "2024-01-19",
        })
    };
    let (minute_status, _, minute) = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), body("1min")),
    )
    .await;
    let (day_status, _, day) = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), body("1d")),
    )
    .await;

    assert_eq!(minute_status, StatusCode::ACCEPTED);
    assert_eq!(day_status, StatusCode::ACCEPTED);
    assert_ne!(minute["job_id"], day["job_id"]);
    let launched = launcher.jobs();
    assert_eq!(launched.len(), 2);
    assert_eq!(launched[0].requests[0].interval.as_deref(), Some("1min"));
    assert_eq!(launched[1].requests[0].interval.as_deref(), Some("1d"));

    let missing = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            json!({
                "asset_class": "equity",
                "data_type": "bars",
                "symbol": "AAPL",
                "start_date": "2024-01-18",
                "end_date": "2024-01-19",
            }),
        ),
    )
    .await;
    assert_eq!(missing.0, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(missing.2["error"]["code"], "validation_error");
    assert_eq!(
        missing.2["error"]["details"][0]["msg"],
        "Value error, interval is required for bars"
    );

    let option_bars = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            json!({
                "asset_class": "option",
                "data_type": "bars",
                "underlying": "SPY",
                "start_date": "2024-01-18",
                "end_date": "2024-01-19",
            }),
        ),
    )
    .await;
    assert_eq!(option_bars.0, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(option_bars.2["error"]["code"], "validation_error");
    assert!(option_bars.2["error"]["details"][0]["msg"]
        .as_str()
        .expect("message")
        .contains("interval is required for bars"));
}

#[tokio::test]
async fn option_bar_ensure_preserves_interval_and_uses_distinct_jobs() {
    let symbol = "SPY240119C00500000";
    let launcher = RecordingAdmission::default();
    let mut state = base_state(shared_catalog([]));
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract(symbol, "SPY", date(1, 19)),
    ])));
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let router = build_app(state);
    let mut ids = Vec::new();
    for interval in ["1min", "1d"] {
        let request = json!({"asset_class":"option","data_type":"bars","interval":interval,
            "symbol":symbol,"start_date":"2024-01-18","end_date":"2024-01-19"});
        let (status, _, body) = send(
            &router,
            post_json("/v1/catalog/ensure", Some(TOKEN), request),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED, "{body}");
        ids.push(body["job_id"].clone());
    }
    assert_ne!(ids[0], ids[1]);
    let launched = launcher.jobs();
    assert_eq!(launched.len(), 2);
    assert_eq!(launched[0].requests[0].interval.as_deref(), Some("1min"));
    assert_eq!(launched[1].requests[0].interval.as_deref(), Some("1d"));
    assert!(launched
        .iter()
        .all(|job| job.requests[0].symbol == symbol
            && job.requests[0].asset_class == AssetClass::Option));
}

#[tokio::test]
async fn admission_returns_queued_and_joins_without_api_owned_execution() {
    let launcher = RecordingAdmission::default();
    let mut state = base_state(shared_catalog([]));
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let router = axum::Router::new().nest("/market-data", build_app(state));
    let admission_path = "/market-data/v1/catalog/ensure";
    let body = equity_body("2024-01-18", "2024-01-19");
    let first = send(
        &router,
        post_json(admission_path, Some(TOKEN), body.clone()),
    )
    .await;
    let second = send(&router, post_json(admission_path, Some(TOKEN), body)).await;
    assert_eq!(first.0, StatusCode::ACCEPTED);
    assert_eq!(first.2, second.2);
    assert_eq!(first.2["state"], "queued");
    assert_eq!(launcher.jobs().len(), 1);
    let id = first.2["job_id"].as_str().unwrap();
    let location = reqwest::Url::parse(&format!("http://api{admission_path}"))
        .unwrap()
        .join(first.1["location"].to_str().unwrap())
        .unwrap();
    assert_eq!(location.path(), format!("/market-data/v1/jobs/{id}"));
    let polled = send(&router, get_auth(location.path())).await;
    assert_eq!(polled.2["state"], "queued");
}

#[tokio::test]
async fn interrupted_admission_reconciles_the_same_retained_job() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let launcher = RecordingAdmission::default();
    let admitted = launcher.launcher();
    let first = Arc::new(AtomicBool::new(true));
    let first_call = first.clone();
    let mut state = base_state(shared_catalog([]));
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(Arc::new(move |job| {
        let admitted = admitted.clone();
        let first = first_call.clone();
        Box::pin(async move {
            admitted(job).await?;
            if first.swap(false, Ordering::SeqCst) {
                Err(lake_contracts::DataError::Io(
                    "acknowledgement interrupted".into(),
                ))
            } else {
                Ok(())
            }
        })
    }));
    let router = build_app(state);
    let body = equity_body("2024-01-18", "2024-01-19");
    assert!(send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), body.clone())
    )
    .await
    .0
    .is_server_error());
    let accepted = send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;
    assert_eq!(accepted.0, StatusCode::ACCEPTED);
    assert_eq!(launcher.jobs().len(), 1);
    assert_eq!(accepted.2["job_id"], launcher.jobs()[0].job_id);
}

#[tokio::test]
async fn missing_admission_acknowledgement_is_a_protocol_failure() {
    let mut state = base_state(shared_catalog([]));
    state.ensure_admission = Some(Arc::new(|_| Box::pin(async { Ok(()) })));
    let result = send(
        &build_app(state),
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2024-01-18", "2024-01-19"),
        ),
    )
    .await;
    assert!(result.0.is_server_error());
}

#[tokio::test]
async fn an_explicit_new_attempt_never_overwrites_a_terminal_job() {
    use lake_catalog::JobRepository;
    let launcher = RecordingAdmission::default();
    let mut state = base_state(shared_catalog([]));
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let router = build_app(state);
    let mut body = equity_body("2024-01-18", "2024-01-19");
    body["job_id"] = json!("ingest-first");
    assert_eq!(
        send(
            &router,
            post_json("/v1/catalog/ensure", Some(TOKEN), body.clone())
        )
        .await
        .2["job_id"],
        "ingest-first"
    );
    let mut original = launcher.records.get("ingest-first").unwrap().unwrap();
    original.state = JobState::Failed;
    launcher.records.upsert(&original).unwrap();
    assert_eq!(
        send(
            &router,
            post_json("/v1/catalog/ensure", Some(TOKEN), body.clone())
        )
        .await
        .2["state"],
        "failed"
    );
    body["job_id"] = json!("ingest-second");
    assert_eq!(
        send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body))
            .await
            .2["state"],
        "queued"
    );
    assert_eq!(
        launcher.records.get("ingest-first").unwrap().unwrap(),
        original
    );
}

#[tokio::test]
async fn option_membership_is_one_durable_job_with_all_selected_contracts() {
    let launcher = RecordingAdmission::default();
    let mut state = base_state(shared_catalog([]));
    state.jobs = launcher.records.clone();
    state.ensure_admission = Some(launcher.launcher());
    let first = "SPY240119C00500000";
    let second = "SPY240119C00510000";
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract(first, "SPY", date(1, 19)),
        contract(second, "SPY", date(1, 19)),
    ])));
    let progress_catalog = state.catalog.clone();
    let router = build_app(state);
    let body = json!({"asset_class":"option","data_type":"trade","contract_symbols":[first,second,first],"start_date":"2024-01-19","end_date":"2024-01-19"});
    let accepted = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), body.clone()),
    )
    .await;
    assert_eq!(accepted.0, StatusCode::ACCEPTED, "{:?}", accepted.2);
    progress_catalog
        .upsert(&complete_record(
            AssetClass::Option,
            DataType::Trade,
            first,
            date(1, 19),
        ))
        .unwrap();
    let joined = send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;
    assert_eq!(joined.0, StatusCode::ACCEPTED, "{:?}", joined.2);
    let jobs = launcher.jobs();
    assert_eq!(jobs.len(), 1);
    let symbols: std::collections::BTreeSet<_> =
        jobs[0].requests.iter().map(|g| g.symbol.as_str()).collect();
    assert_eq!(symbols, std::collections::BTreeSet::from([first, second]));
    assert_eq!(accepted.2["job_id"], jobs[0].job_id);
}

#[tokio::test]
async fn option_resolution_acceptance_preserves_the_hosts_mount_prefix() {
    let mut state = base_state(shared_catalog([]));
    state.jobs = Arc::new(MemoryJobRepository::from_records([JobRecord::new(
        "option-one",
        JobState::Queued,
        as_of(),
        as_of(),
        None,
    )
    .unwrap()]));
    state.option_resolver = Some(Arc::new(|_, id| {
        Box::pin(async move { Err(lake_contracts::DataError::PendingJob(id.unwrap())) })
    }));
    let router = axum::Router::new().nest("/market-data", build_app(state));
    let path = "/market-data/v1/options/resolve";
    let accepted = send(
        &router,
        post_json(
            path,
            Some(TOKEN),
            json!({
                "job_id":"option-one", "underlying":"SPY", "right":"call",
                "as_of":"2024-01-18T21:00:00Z", "expiration":"2024-02-16", "strike":"100"
            }),
        ),
    )
    .await;
    assert_eq!(accepted.0, StatusCode::ACCEPTED, "{}", accepted.2);
    let location = reqwest::Url::parse(&format!("http://api{path}"))
        .unwrap()
        .join(accepted.1["location"].to_str().unwrap())
        .unwrap();
    assert_eq!(location.path(), "/market-data/v1/jobs/option-one");
    let polled = send(&router, get_auth(location.path())).await;
    assert_eq!(polled.0, StatusCode::OK);
    assert_eq!(polled.2["job_id"], "option-one");
}
