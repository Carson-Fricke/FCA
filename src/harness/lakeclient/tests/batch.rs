//! Bounded parallel ensure/read fan-out over the fake lake.
//!
//! The batch helpers must issue every spec, keep the spec order in their
//! results, and stay a no-op for an empty request list. Concurrency itself is
//! not timing-asserted here; the ordering contract is what callers rely on for
//! deterministic failure reporting.

use harness_lakeclient::testing::bars_parquet;
use harness_lakeclient::testing::FakeBarRow;
use harness_lakeclient::testing::FakeLakeConfig;
use harness_lakeclient::testing::FakeLakeServer;
use harness_lakeclient::BlockingLakeClient;
use harness_lakeclient::EnsureSpec;
use harness_lakeclient::LakeClientConfig;
use harness_lakeclient::ReadSpec;

const TOKEN: &str = "batch-token";

fn client(server: &FakeLakeServer) -> BlockingLakeClient {
    BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), TOKEN).expect("lake client config"),
    )
    .expect("blocking lake client")
}

fn satisfied_ensure_config() -> FakeLakeConfig {
    let mut config = FakeLakeConfig::new(TOKEN);
    config.ensure_bodies = vec![(
        200,
        serde_json::json!({
            "job_id": "fake-job",
            "state": "complete",
            "entries": [{
                "session_date": "2024-01-02",
                "is_partial": false,
                "status": "complete"
            }]
        }),
    )];
    config
}

fn ensure_spec(symbol: &str, data_type: &str) -> EnsureSpec {
    EnsureSpec::new("equity", data_type, symbol, "2024-01-02", "2024-01-02")
}

fn read_spec(symbol: &str, data_type: &str) -> ReadSpec {
    let spec = ReadSpec::new("equity", data_type, symbol, "2024-01-02", "2024-01-02");
    if data_type == "bars" {
        spec.with_interval("1min")
    } else {
        spec
    }
}

fn ensured_symbols(server: &FakeLakeServer) -> Vec<String> {
    let mut symbols: Vec<String> = server
        .requests()
        .iter()
        .filter(|request| request.path == "/v1/catalog/ensure")
        .filter_map(|request| request.body.as_deref())
        .filter_map(|body| {
            let text = String::from_utf8_lossy(body).into_owned();
            ["SPY", "QQQ", "IWM"]
                .into_iter()
                .find(|symbol| text.contains(&format!("\"{symbol}\"")))
                .map(str::to_string)
        })
        .collect();
    symbols.sort();
    symbols
}

#[test]
fn ensure_many_issues_every_spec_and_keeps_order() {
    let server = FakeLakeServer::start(satisfied_ensure_config());
    let client = client(&server);
    let specs = vec![ensure_spec("SPY", "bars"), ensure_spec("QQQ", "bars")];

    let results = client.ensure_many(&specs);

    assert_eq!(results.len(), 2);
    assert!(results.iter().all(Result::is_ok), "{results:?}");
    assert_eq!(
        ensured_symbols(&server),
        vec!["QQQ".to_string(), "SPY".to_string()]
    );
}

#[test]
fn read_many_returns_each_specs_bodies_in_order() {
    let mut config = satisfied_ensure_config();
    config.partitions_by_type = std::collections::BTreeMap::from([(
        "bars".to_string(),
        bars_parquet(
            "SPY",
            &[FakeBarRow::new(
                1_704_153_600_000_000_000,
                "1",
                "2",
                "0.5",
                "1.5",
                "100",
            )],
        ),
    )]);
    let server = FakeLakeServer::start(config);
    let client = client(&server);
    let specs = vec![read_spec("SPY", "bars"), read_spec("SPY", "trade")];

    let results = client.read_many(&specs);

    assert_eq!(results.len(), 2);
    let bars = results[0].as_ref().expect("bars read");
    assert_eq!(bars.iter().map(|batch| batch.num_rows()).sum::<usize>(), 1);
    let trades = results[1].as_ref().expect("trade read");
    assert!(trades.is_empty());
}

#[test]
fn empty_batches_issue_no_requests() {
    let server = FakeLakeServer::start(satisfied_ensure_config());
    let client = client(&server);

    assert!(client.ensure_many(&[]).is_empty());
    assert!(client.read_many(&[]).is_empty());
    assert!(server.requests().is_empty());
}

#[test]
fn environment_url_overrides_route_authenticated_requests() {
    // Each process has its own environment; parallel tests never mutate one
    // another's configuration and these checks cannot reach the real lake.
    const CHILD: &str = "FCA_LAKE_ENV_TEST_CHILD";
    if std::env::var_os(CHILD).is_some() {
        let client = BlockingLakeClient::from_env().expect("environment client");
        client.ensure(&ensure_spec("SPY", "bars")).unwrap();
        return;
    }
    let server = FakeLakeServer::start(satisfied_ensure_config());
    for (primary, secondary) in [
        (server.base_url(), "http://127.0.0.1:1"),
        (" ", server.base_url()),
    ] {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "environment_url_overrides_route_authenticated_requests",
            ])
            .env(CHILD, "1")
            .env("LAKE_API_URL", primary)
            .env("LAKE_API_BASE_URL", secondary)
            .env("LAKE_API__BEARER_TOKEN", TOKEN)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
    }
    assert_eq!(
        server
            .requests()
            .iter()
            .filter(|r| r.path == "/v1/catalog/ensure")
            .count(),
        2
    );
}
