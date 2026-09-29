//! Client coverage against a local fake of the frozen market-lake routes.
//!
//! Every test binds the `test-support` fake on loopback, asserts the exact URLs
//! and bearer header, and decodes recorded JSON and Arrow IPC fixtures. No
//! network and no lake files are touched.

use harness_lakeclient::testing::{
    bars_stream, no_data_body, FakeBarRow, FakeLakeConfig, FakeLakeServer,
};
use harness_lakeclient::{
    BlockingLakeClient, EnsureSpec, LakeClient, LakeClientConfig, LakeClientError, ReadSpec,
};
use serde_json::{json, Value};

const TOKEN: &str = "test-token";

fn config(server: &FakeLakeServer) -> LakeClientConfig {
    LakeClientConfig::new(server.base_url(), TOKEN).expect("client config")
}

fn trade_spec() -> EnsureSpec {
    EnsureSpec::new("equity", "trade", "SPY", "2024-01-02", "2024-01-03")
}

fn complete_coverage() -> Value {
    json!({
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "SPY",
        "start_date": "2024-01-02",
        "end_date": "2024-01-03",
        "entries": [
            {"session_date": "2024-01-02", "is_partial": false, "status": "complete"},
            {"session_date": "2024-01-03", "is_partial": false, "status": "empty"}
        ]
    })
}

fn missing_coverage() -> Value {
    json!({
        "asset_class": "equity",
        "data_type": "trade",
        "symbol": "SPY",
        "start_date": "2024-01-02",
        "end_date": "2024-01-03",
        "entries": [
            {"session_date": "2024-01-02", "is_partial": false, "status": "missing"}
        ]
    })
}

#[tokio::test]
async fn ensure_returns_immediately_when_the_window_is_already_covered() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        ensure_bodies: vec![(200, complete_coverage())],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    client.ensure(&trade_spec()).await.expect("covered ensure");

    let requests = server.requests();
    assert_eq!(requests.len(), 1, "one ensure request, no polling");
    assert_eq!(requests[0].method, "POST");
    assert_eq!(requests[0].path, "/v1/catalog/ensure");
    assert_eq!(
        requests[0].authorization.as_deref(),
        Some("Bearer test-token")
    );
    let body: Value =
        serde_json::from_slice(requests[0].body.as_deref().expect("ensure body")).expect("json");
    assert_eq!(
        body,
        json!({
            "asset_class": "equity",
            "data_type": "trade",
            "symbol": "SPY",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
        })
    );
}

#[tokio::test]
async fn ensure_polls_job_and_coverage_until_satisfied() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        ensure_bodies: vec![(200, json!({"job_id": "job-1", "state": "running"}))],
        job_bodies: vec![
            (200, json!({"job_id": "job-1", "state": "running"})),
            (200, json!({"job_id": "job-1", "state": "complete"})),
        ],
        coverage_bodies: vec![(200, missing_coverage()), (200, complete_coverage())],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    client.ensure(&trade_spec()).await.expect("polled ensure");

    let requests = server.requests();
    let sequence: Vec<(&str, &str)> = requests
        .iter()
        .map(|request| (request.method.as_str(), request.path.as_str()))
        .collect();
    assert_eq!(
        sequence,
        [
            ("POST", "/v1/catalog/ensure"),
            ("GET", "/v1/jobs/job-1"),
            ("GET", "/v1/catalog/coverage"),
            ("GET", "/v1/jobs/job-1"),
            ("GET", "/v1/catalog/coverage"),
        ]
    );
    let coverage_query = &requests[2].query;
    assert_eq!(
        coverage_query.get("symbol").map(String::as_str),
        Some("SPY")
    );
    assert_eq!(
        coverage_query.get("start_date").map(String::as_str),
        Some("2024-01-02")
    );
    assert_eq!(
        coverage_query.get("end_date").map(String::as_str),
        Some("2024-01-03")
    );
    assert!(coverage_query.get("interval").is_none());
}

#[tokio::test]
async fn ensure_sends_the_bars_interval_on_ensure_and_coverage() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        ensure_bodies: vec![(200, json!({"job_id": "job-1", "state": "running"}))],
        coverage_bodies: vec![(
            200,
            json!({
                "asset_class": "equity",
                "data_type": "bars",
                "symbol": "SPY",
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "entries": [{"session_date": "2024-01-02", "status": "complete"}]
            }),
        )],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");
    let spec =
        EnsureSpec::new("equity", "bars", "SPY", "2024-01-02", "2024-01-02").with_interval("1min");

    client.ensure(&spec).await.expect("bars ensure");

    let requests = server.requests();
    let ensure_body: Value =
        serde_json::from_slice(requests[0].body.as_deref().expect("body")).expect("json");
    assert_eq!(ensure_body["data_type"], "bars");
    assert_eq!(ensure_body["interval"], "1min");
    assert_eq!(
        requests
            .iter()
            .find(|request| request.path == "/v1/catalog/coverage")
            .expect("coverage request")
            .query
            .get("interval")
            .map(String::as_str),
        Some("1min")
    );
}

#[tokio::test]
async fn ensure_fails_when_a_settled_job_leaves_coverage_short() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        ensure_bodies: vec![(200, json!({"job_id": "job-1", "state": "running"}))],
        job_bodies: vec![(200, json!({"job_id": "job-1", "state": "complete"}))],
        coverage_bodies: vec![(
            200,
            json!({
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "SPY",
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
                "entries": [{
                    "session_date": "2024-01-02",
                    "is_partial": false,
                    "status": "partial",
                    "symbol": "SPY",
                    "data_type": "trade"
                }]
            }),
        )],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let error = client
        .ensure(&trade_spec())
        .await
        .expect_err("short window");
    let message = error.to_string();
    assert!(
        message.starts_with(
            "market-lake could not fetch SPY equity/trade for 2024-01-02..2024-01-03:"
        ),
        "{message}"
    );
    assert!(
        message.contains("market-lake could not complete the period"),
        "{message}"
    );
    assert!(
        message.contains("the closed session was left partial"),
        "{message}"
    );
    assert_eq!(
        server.requests().len(),
        3,
        "no sleep after a settled verdict"
    );
}

#[tokio::test]
async fn ensure_fails_when_the_job_failed() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        ensure_bodies: vec![(200, json!({"job_id": "job-1", "state": "running"}))],
        job_bodies: vec![(
            200,
            json!({
                "job_id": "job-1",
                "state": "failed",
                "error_message": "provider rejected the window"
            }),
        )],
        coverage_bodies: vec![(200, missing_coverage())],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let error = client.ensure(&trade_spec()).await.expect_err("failed job");
    let message = error.to_string();
    assert!(
        message.contains("market-lake ensure job failed: provider rejected the window"),
        "{message}"
    );
}

#[tokio::test]
async fn coverage_and_job_decode_typed_reports() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        coverage_bodies: vec![(200, complete_coverage())],
        job_bodies: vec![(
            200,
            json!({
                "job_id": "job-1",
                "state": "failed",
                "error_message": "boom"
            }),
        )],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let report = client.coverage(&trade_spec()).await.expect("coverage");
    assert_eq!(report.asset_class, "equity");
    assert_eq!(report.data_type, "trade");
    assert_eq!(report.symbol, "SPY");
    assert_eq!(report.entries.len(), 2);
    assert_eq!(report.entries[0].session_date, "2024-01-02");
    assert_eq!(report.entries[0].status, "complete");

    let status = client.job("job-1").await.expect("job");
    assert_eq!(status.job_id, "job-1");
    assert_eq!(status.state, "failed");
    assert_eq!(status.error_message.as_deref(), Some("boom"));
}

#[tokio::test]
async fn earliest_complete_session_is_the_minimum_complete_date() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        coverage_bodies: vec![(
            200,
            json!({
                "asset_class": "equity",
                "data_type": "trade",
                "symbol": "SPY",
                "start_date": "2015-12-09",
                "end_date": "2020-03-13",
                "entries": [
                    {"session_date": "2005-01-03", "status": "empty"},
                    {"session_date": "2015-12-09", "status": "missing"},
                    {"session_date": "2015-12-10", "status": "complete"},
                    {"session_date": "2015-12-11", "status": "complete"},
                    {"session_date": "2016-01-04", "status": "error"}
                ]
            }),
        )],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let earliest = client
        .earliest_complete_session(&trade_spec())
        .await
        .expect("coverage");
    assert_eq!(earliest, Some("2015-12-10".to_string()));
}

#[tokio::test]
async fn read_arrow_decodes_the_frozen_bars_stream() {
    let rows = vec![
        FakeBarRow::new(
            1_704_204_000_000_000_000,
            "100.5",
            "101.25",
            "99.75",
            "101",
            "123456",
        ),
        FakeBarRow::new(
            1_704_290_400_000_000_000,
            "101",
            "102.5",
            "100.25",
            "102.5",
            "654321",
        ),
    ];
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies: vec![(200, bars_stream("SPY", &rows))],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");
    let spec =
        ReadSpec::new("equity", "bars", "SPY", "2024-01-02", "2024-01-03").with_interval("1d");

    let batches = client.read_arrow(&spec).await.expect("arrow read");
    assert_eq!(batches.len(), 1);
    assert_eq!(batches[0].num_rows(), 2);
    assert_eq!(batches[0].schema().field(0).name(), "timestamp");
    assert_eq!(batches[0].schema().field(1).name(), "symbol");

    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].path, "/v1/read");
    assert_eq!(
        requests[0].query.get("data_type").map(String::as_str),
        Some("bars")
    );
    assert_eq!(
        requests[0].query.get("interval").map(String::as_str),
        Some("1d")
    );
    assert_eq!(
        requests[0].query.get("start_date").map(String::as_str),
        Some("2024-01-02")
    );
    assert_eq!(
        requests[0].query.get("end_date").map(String::as_str),
        Some("2024-01-03")
    );
}

#[tokio::test]
async fn read_arrow_maps_no_data_to_an_empty_result() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies: vec![(404, no_data_body())],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let batches = client
        .read_arrow(&ReadSpec::new(
            "equity",
            "bars",
            "SPY",
            "2024-01-02",
            "2024-01-03",
        ))
        .await
        .expect("no_data is an empty read");
    assert!(batches.is_empty());
}

#[tokio::test]
async fn read_arrow_surfaces_a_malformed_stream_as_a_decode_error() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies: vec![(200, b"not an arrow stream".to_vec())],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let error = client
        .read_arrow(&ReadSpec::new(
            "equity",
            "bars",
            "SPY",
            "2024-01-02",
            "2024-01-03",
        ))
        .await
        .expect_err("malformed stream");
    assert!(matches!(error, LakeClientError::Decode(_)), "{error:?}");
    assert!(error.to_string().contains("Arrow IPC"), "{error}");
}

#[tokio::test]
async fn reference_routes_decode_universe_and_facts() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        universe_body: json!({"as_of": "2024-01-02", "symbols": ["AAPL", "MSFT"]}),
        facts_body: json!({
            "as_of": "2024-01-02",
            "facts": {
                "AAPL": {"sector": "technology", "momentum": "9.5"},
                "MSFT": {"sector": "technology"}
            }
        }),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let symbols = client
        .reference_universe("2024-01-02")
        .await
        .expect("universe");
    assert_eq!(symbols, ["AAPL", "MSFT"]);

    let facts = client
        .reference_facts(&["AAPL".to_string(), "MSFT".to_string()], "2024-01-02")
        .await
        .expect("facts");
    assert_eq!(
        facts
            .get("AAPL")
            .and_then(|entry| entry.get("sector"))
            .map(String::as_str),
        Some("technology")
    );
    assert_eq!(facts.keys().collect::<Vec<_>>(), ["AAPL", "MSFT"]);

    let requests = server.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].path, "/v1/reference/universe");
    assert_eq!(
        requests[0].query.get("as_of").map(String::as_str),
        Some("2024-01-02")
    );
    assert_eq!(requests[1].path, "/v1/reference/facts");
    assert_eq!(
        requests[1].query.get("symbols").map(String::as_str),
        Some("AAPL,MSFT")
    );
    assert_eq!(
        requests[1].authorization.as_deref(),
        Some("Bearer test-token")
    );
}

#[tokio::test]
async fn http_errors_carry_the_status_and_error_code() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        coverage_bodies: vec![(
            422,
            json!({
                "error": {"code": "validation_error", "message": "interval is required for bars"}
            }),
        )],
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let error = client.coverage(&trade_spec()).await.expect_err("422");
    assert_eq!(
        error,
        LakeClientError::Http {
            status: 422,
            code: Some("validation_error".to_string()),
            body: serde_json::to_string(&json!({
                "error": {"code": "validation_error", "message": "interval is required for bars"}
            }))
            .expect("json"),
        }
    );
    assert!(error.to_string().starts_with("HTTP 422: "), "{error}");
}

#[tokio::test]
async fn a_missing_bearer_token_is_rejected_by_the_lake() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        expected_token: "the-real-token".to_string(),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = LakeClient::new(config(&server)).expect("client");

    let error = client
        .reference_universe("2024-01-02")
        .await
        .expect_err("401");
    assert_eq!(
        error,
        LakeClientError::Http {
            status: 401,
            code: Some("unauthorized".to_string()),
            body: "{\"error\":{\"code\":\"unauthorized\",\"message\":\"invalid bearer token\"}}"
                .to_string(),
        }
    );
}

#[test]
fn debug_output_redacts_the_bearer_token() {
    let config = LakeClientConfig::new("http://127.0.0.1:8002", "super-secret").expect("config");
    let client = LakeClient::new(config.clone()).expect("client");
    for rendered in [format!("{config:?}"), format!("{client:?}")] {
        assert!(!rendered.contains("super-secret"), "{rendered}");
        assert!(rendered.contains("<redacted>"), "{rendered}");
    }
}

#[test]
fn from_env_requires_a_bearer_token_and_prefers_lake_api_url() {
    let saved = [
        (
            "LAKE_API__BEARER_TOKEN",
            std::env::var("LAKE_API__BEARER_TOKEN").ok(),
        ),
        ("LAKE_API_URL", std::env::var("LAKE_API_URL").ok()),
        ("LAKE_API_BASE_URL", std::env::var("LAKE_API_BASE_URL").ok()),
    ];
    std::env::remove_var("LAKE_API__BEARER_TOKEN");
    std::env::remove_var("LAKE_API_URL");
    std::env::remove_var("LAKE_API_BASE_URL");

    let error = LakeClientConfig::from_env().expect_err("token is required");
    assert!(
        error.to_string().contains("LAKE_API__BEARER_TOKEN"),
        "{error}"
    );

    std::env::set_var("LAKE_API__BEARER_TOKEN", "env-token");
    std::env::set_var("LAKE_API_BASE_URL", "http://127.0.0.1:9998/");
    let from_base = LakeClientConfig::from_env().expect("base URL fallback");
    assert_eq!(from_base.base_url(), "http://127.0.0.1:9998");

    std::env::set_var("LAKE_API_URL", "http://127.0.0.1:9999/");
    let from_url = LakeClientConfig::from_env().expect("url preference");
    assert_eq!(from_url.base_url(), "http://127.0.0.1:9999");

    for (name, value) in saved {
        match value {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
    }
}

#[test]
fn blocking_client_drives_the_async_client() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        universe_body: json!({"as_of": "2024-01-02", "symbols": ["SPY"]}),
        ..FakeLakeConfig::new(TOKEN)
    });
    let client = BlockingLakeClient::new(config(&server)).expect("blocking client");

    let symbols = client.reference_universe("2024-01-02").expect("universe");
    assert_eq!(symbols, ["SPY"]);
    assert_eq!(
        server.requests()[0].authorization.as_deref(),
        Some("Bearer test-token")
    );
}
