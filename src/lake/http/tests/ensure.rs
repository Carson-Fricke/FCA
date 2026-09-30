//! Ensure and job-polling route behavior.

mod support;

use std::sync::Arc;
use std::time::Duration;

use axum::http::StatusCode;
use lake_catalog::{
    CoverageRecord, CoverageStatus, JobRecord, JobState, MemoryCatalogRepository,
    MemoryContractRepository, MemoryJobRepository,
};
use lake_contracts::{AssetClass, DataType, Date};
use lake_coverage::EnsureJobLauncher;
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
    let launcher = RecordingLauncher::default();
    let mut state = base_state(catalog);
    state.ensure_launcher = Some(launcher.launcher());
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
async fn missing_ensure_returns_synchronous_completion_when_the_launcher_fills() {
    let catalog = shared_catalog([]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.ensure_launcher = Some(filling_launcher(
        Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>
    ));
    let router = build_app(state);

    let (status, headers, body) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2024-01-18", "2024-01-18"),
        ),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["entries"][0]["status"], "complete");
    assert!(headers.get("location").is_none());
}

#[tokio::test]
async fn missing_ensure_persists_a_stable_job_and_polls_to_completion() {
    let catalog = shared_catalog([]);
    let launcher = RecordingLauncher::default();
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.ensure_launcher = Some(launcher.launcher());
    let router = build_app(state);

    let request = equity_body("2024-01-18", "2024-01-19");
    let (first_status, first_headers, first) = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), request.clone()),
    )
    .await;
    let (second_status, _, second) = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), request),
    )
    .await;

    assert_eq!(first_status, StatusCode::ACCEPTED);
    assert_eq!(second_status, StatusCode::ACCEPTED);
    assert_eq!(first, second);
    assert_eq!(first["state"], "complete");
    assert_eq!(
        first_headers["location"],
        format!("/v1/jobs/{}", first["job_id"].as_str().expect("job id"))
    );

    let launched = launcher.jobs();
    assert_eq!(launched.len(), 2);
    assert_eq!(
        launched[0].job_id,
        first["job_id"].as_str().expect("job id")
    );
    assert_eq!(launched[0].gaps.len(), 2);
    assert_eq!(launched[0].request.symbol, "AAPL");

    let job_id = first["job_id"].as_str().expect("job id");
    let (poll_status, _, polled) = send(&router, get_auth(&format!("/v1/jobs/{job_id}"))).await;
    assert_eq!(poll_status, StatusCode::OK);
    assert_eq!(
        polled,
        json!({
            "job_id": job_id,
            "state": "complete",
            "created_at": "2024-01-22T12:00:00Z",
            "updated_at": "2024-01-22T12:00:00Z",
            "error_message": null,
            "progress": null,
        })
    );
}

#[tokio::test]
async fn an_in_flight_job_short_circuits_without_relaunching() {
    let catalog = shared_catalog([]);
    let blocking: EnsureJobLauncher = Arc::new(|_job| {
        Box::pin(async {
            tokio::time::sleep(Duration::from_secs(30)).await;
            Ok(())
        })
    });
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.ensure_launcher = Some(blocking);
    state.ensure_wait_deadline_seconds = 0.01;
    let router = build_app_with(
        state,
        lake_http::LakeApiConfig {
            bearer_token: Some(TOKEN.to_string()),
            ensure_wait_deadline_seconds: 0.01,
        },
    );

    let started = std::time::Instant::now();
    let (first_status, _, first) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2024-01-18", "2024-01-19"),
        ),
    )
    .await;
    let elapsed = started.elapsed();
    let (second_status, _, second) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2024-01-18", "2024-01-19"),
        ),
    )
    .await;

    assert_eq!(first_status, StatusCode::ACCEPTED);
    assert_eq!(second_status, StatusCode::ACCEPTED);
    assert_eq!(first, second);
    assert_eq!(first["state"], "running");
    assert!(elapsed < Duration::from_secs(1));
}

#[tokio::test]
async fn a_launcher_failure_is_persisted_as_a_failed_job() {
    let catalog = shared_catalog([]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.ensure_launcher = Some(failing_launcher("launcher failed"));
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        post_json(
            "/v1/catalog/ensure",
            Some(TOKEN),
            equity_body("2024-01-18", "2024-01-19"),
        ),
    )
    .await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "failed");
    let job_id = body["job_id"].as_str().expect("job id");
    let (_, _, polled) = send(&router, get_auth(&format!("/v1/jobs/{job_id}"))).await;
    assert_eq!(polled["state"], "failed");
    assert_eq!(polled["error_message"], "launcher failed");
}

#[tokio::test]
async fn a_terminal_unsatisfied_job_is_superseded_and_relaunched() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([equity_record(
        date(1, 18),
        CoverageStatus::Error,
    )]));
    let launcher = RecordingLauncher::default();
    let mut state = base_state(catalog);
    state.ensure_launcher = Some(launcher.launcher());
    let router = build_app(state);

    let body = equity_body("2024-01-18", "2024-01-18");
    let (first_status, _, first) = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), body.clone()),
    )
    .await;
    let (second_status, _, second) =
        send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;

    assert_eq!(first_status, StatusCode::ACCEPTED);
    assert_eq!(second_status, StatusCode::ACCEPTED);
    assert_eq!(first, second);
    let launched = launcher.jobs();
    assert_eq!(launched.len(), 2);
    assert_eq!(launched[0].gaps[0].state.value(), "error");
    let job_id = first["job_id"].as_str().expect("job id");
    let (_, _, polled) = send(&router, get_auth(&format!("/v1/jobs/{job_id}"))).await;
    assert_eq!(polled["state"], "complete");
    assert!(polled["error_message"].is_null());
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
    let launcher = RecordingLauncher::default();
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.ensure_launcher = Some(launcher.launcher());
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
    assert_eq!(body["state"], "complete");
    let launched = launcher.jobs();
    assert_eq!(launched.len(), 1);
    assert!(launched[0].gaps.len() > 366);
    assert_eq!(
        launched[0].gaps[0].session_date(),
        Date::from_ymd(2022, 1, 3).expect("valid date")
    );
    assert_eq!(
        launched[0].gaps.last().expect("last gap").session_date(),
        Date::from_ymd(2024, 1, 19).expect("valid date")
    );
}

#[tokio::test]
async fn option_ensure_uses_selected_osi_contracts_and_a_stable_job_id() {
    let contract_symbol = "SPY240119C00500000";
    let catalog = shared_catalog([]);
    let launcher = RecordingLauncher::default();
    let jobs = shared_jobs([]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn lake_catalog::CatalogRepository>);
    state.jobs = Arc::clone(&jobs) as Arc<dyn lake_catalog::JobRepository>;
    state.ensure_launcher = Some(launcher.launcher());
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
    let (first_status, _, first) = send(
        &router,
        post_json("/v1/catalog/ensure", Some(TOKEN), body.clone()),
    )
    .await;
    let (second_status, _, second) =
        send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;

    assert_eq!(first_status, StatusCode::ACCEPTED);
    assert_eq!(second_status, StatusCode::ACCEPTED);
    assert_eq!(first, second);
    // Byte-pinned SHA-256 identifier for the selected membership.
    assert_eq!(
        first["job_id"],
        "ensure-f4b653ffbbc0a5b6060732437f7adc25b69a49335ca532f2b4fddb84ec9595e8"
    );
    assert_eq!(first["state"], "complete");
    let launched = launcher.jobs();
    assert_eq!(launched.len(), 2);
    assert_eq!(launched[0].request.symbol, contract_symbol);
    assert_eq!(
        launched[0]
            .gaps
            .iter()
            .map(|gap| gap.symbol.clone())
            .collect::<Vec<_>>(),
        vec![contract_symbol.to_string()]
    );
    assert_eq!(
        launched[0].job_id,
        format!(
            "{}-{contract_symbol}",
            first["job_id"].as_str().expect("job id")
        )
    );
}

#[tokio::test]
async fn option_ensure_launches_one_deduplicated_job_per_contract() {
    let first_symbol = "SPY240119C00500000";
    let second_symbol = "SPY240119C00510000";
    let mut state = base_state(Arc::new(MemoryCatalogRepository::new()));
    let launcher = RecordingLauncher::default();
    state.ensure_launcher = Some(launcher.launcher());
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract(first_symbol, "SPY", date(1, 19)),
        contract(second_symbol, "SPY", date(1, 19)),
    ])));
    let router = build_app(state);

    let body = json!({
        "asset_class": "option",
        "data_type": "trade",
        "contract_symbols": [first_symbol, second_symbol, first_symbol],
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
    });
    let (status, _, response) =
        send(&router, post_json("/v1/catalog/ensure", Some(TOKEN), body)).await;

    assert_eq!(status, StatusCode::ACCEPTED);
    let job_id = response["job_id"].as_str().expect("job id");
    let launched = launcher.jobs();
    assert_eq!(
        launched
            .iter()
            .map(|job| job.job_id.clone())
            .collect::<Vec<_>>(),
        vec![
            format!("{job_id}-{first_symbol}"),
            format!("{job_id}-{second_symbol}"),
        ]
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
    let launcher = RecordingLauncher::default();
    let mut state = base_state(catalog);
    state.ensure_launcher = Some(launcher.launcher());
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
    let launcher = RecordingLauncher::default();
    let mut state = base_state(catalog);
    state.ensure_launcher = Some(launcher.launcher());
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
    assert_eq!(launched[0].request.interval.as_deref(), Some("1min"));
    assert_eq!(launched[1].request.interval.as_deref(), Some("1d"));

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
        .contains("not supported for options"));
}
