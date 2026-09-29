//! Hermetic tests for the hosted Azure composition, ported from
//! `tests/lake/hosted/test_runtime.py`. No live Azure access: the Azure
//! boundaries are constructed without requests and the ingest side effect is
//! replaced with a recording seam.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use futures_util::future::BoxFuture;
use lake_catalog::JobRepository as _;
use lake_contracts::{AssetClass, BarInterval, Date, DateTime};
use lake_hosted::{hosted_overrides, HostedEnsureError, HostedRuntime, IngestBars, IngestSessions};

fn environment() -> BTreeMap<String, String> {
    [
        (
            "LAKE_STORAGE_ACCOUNT_NAME".to_string(),
            "hostedaccount".to_string(),
        ),
        (
            "AZURE_CLIENT_ID".to_string(),
            "00000000-0000-0000-0000-000000000000".to_string(),
        ),
        ("LAKE_PROVIDER__API_KEY".to_string(), "fake-key".to_string()),
        (
            "LAKE_PROVIDER__API_SECRET".to_string(),
            "fake-secret".to_string(),
        ),
    ]
    .into_iter()
    .collect()
}

fn fixed_clock() -> Arc<dyn Fn() -> DateTime + Send + Sync> {
    Arc::new(|| DateTime::from_ymd_hms(2026, 9, 4, 12, 0, 0, 0).expect("clock"))
}

fn runtime(environ: &BTreeMap<String, String>) -> HostedRuntime {
    HostedRuntime::from_environment(environ, Some(fixed_clock())).expect("runtime")
}

fn equity_spec(start: &str, end: &str) -> lake_coverage::EnsureSpec {
    lake_coverage::EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: lake_contracts::DataType::Trade,
        symbol: "aapl".to_string(),
        start_date: Date::parse(start).expect("start"),
        end_date: Date::parse(end).expect("end"),
        interval: None,
    }
}

fn crypto_spec() -> lake_coverage::EnsureSpec {
    lake_coverage::EnsureSpec {
        asset_class: AssetClass::Crypto,
        data_type: lake_contracts::DataType::Trade,
        symbol: "BTC/USD".to_string(),
        start_date: Date::parse("2026-08-28").expect("start"),
        end_date: Date::parse("2026-08-28").expect("end"),
        interval: None,
    }
}

type Ingested = Arc<Mutex<Vec<(String, Date)>>>;

fn recording_ingest(record: Ingested) -> IngestSessions {
    Arc::new(
        move |symbol: String, sessions: Vec<lake_coverage::CoverageSession>| {
            let record = Arc::clone(&record);
            Box::pin(async move {
                for session in &sessions {
                    record
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .push((symbol.clone(), session.session_date));
                }
                Ok(())
            }) as BoxFuture<'static, Result<(), HostedEnsureError>>
        },
    )
}

#[test]
fn hosted_overrides_are_none_without_storage_environment() {
    let environ = BTreeMap::new();
    assert!(hosted_overrides(&environ, None)
        .expect("overrides")
        .is_none());
}

#[test]
fn hosted_runtime_builds_azure_boundaries_from_environment() {
    let runtime = runtime(&environment());

    // The concrete Azure boundary types are compile-checked by the accessors;
    // the environment wiring reaches construction without network access.
    assert_eq!(
        runtime.blob_url(),
        "https://hostedaccount.blob.core.windows.net"
    );
    assert_eq!(runtime.container_name(), "lake");
}

#[test]
fn hosted_overrides_wire_the_hosted_boundaries() {
    let environ = environment();
    let overrides = hosted_overrides(&environ, Some(fixed_clock()))
        .expect("overrides")
        .expect("hosted");

    // The overrides carry the hosted catalog and job repository plus callable
    // ensure boundaries.
    let _: lake_query::EnsureCallback = overrides.ensure_callback;
    let _: lake_coverage::EnsureJobLauncher = overrides.job_launcher;
    // A read of the hosted catalog is served by the Azure repository without
    // a live round trip for a miss-free construction assertion.
    let _ = (&overrides.catalog, &overrides.jobs, &overrides.sas_issuer);
}

#[tokio::test]
async fn ensure_sessions_ingests_each_closed_equity_session() {
    let environ = environment();
    let ingested: Ingested = Arc::new(Mutex::new(Vec::new()));
    let runtime =
        Arc::new(runtime(&environ).with_ingest_override(recording_ingest(Arc::clone(&ingested))));

    runtime
        .ensure_sessions(&equity_spec("2026-08-28", "2026-08-31"))
        .await
        .expect("ensure");

    let ingested = ingested
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    assert_eq!(
        ingested,
        vec![
            ("AAPL".to_string(), Date::parse("2026-08-28").expect("date")),
            ("AAPL".to_string(), Date::parse("2026-08-31").expect("date")),
        ]
    );
}

#[tokio::test]
async fn ensure_sessions_ensures_trades_then_bars() {
    let environ = environment();
    let ingested: Ingested = Arc::new(Mutex::new(Vec::new()));
    type IngestedBars = Arc<Mutex<Vec<(String, BarInterval, Vec<Date>)>>>;
    let ingested_bars: IngestedBars = Arc::new(Mutex::new(Vec::new()));
    let bars_recorder = Arc::clone(&ingested_bars);
    let bars_override: IngestBars = Arc::new(
        move |symbol: String,
              interval: BarInterval,
              sessions: Vec<lake_coverage::CoverageSession>| {
            let record = Arc::clone(&bars_recorder);
            Box::pin(async move {
                record
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .push((
                        symbol,
                        interval,
                        sessions
                            .iter()
                            .map(|session| session.session_date)
                            .collect(),
                    ));
                Ok(())
            }) as BoxFuture<'static, Result<(), HostedEnsureError>>
        },
    );
    let runtime = Arc::new(
        runtime(&environ)
            .with_ingest_override(recording_ingest(Arc::clone(&ingested)))
            .with_bars_ingest_override(bars_override),
    );

    let bars = lake_coverage::EnsureSpec {
        data_type: lake_contracts::DataType::Bars,
        interval: Some("1d".to_string()),
        ..equity_spec("2026-08-28", "2026-08-31")
    };
    runtime.ensure_sessions(&bars).await.expect("ensure");

    let ingested = ingested
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    assert_eq!(
        ingested,
        vec![
            ("AAPL".to_string(), Date::parse("2026-08-28").expect("date")),
            ("AAPL".to_string(), Date::parse("2026-08-31").expect("date")),
        ],
        "trades are ensured before bars"
    );
    let bars_calls = ingested_bars
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    assert_eq!(bars_calls.len(), 1);
    assert_eq!(bars_calls[0].0, "AAPL");
    assert_eq!(bars_calls[0].1, BarInterval::OneDay);
    assert_eq!(
        bars_calls[0].2,
        vec![
            Date::parse("2026-08-28").expect("date"),
            Date::parse("2026-08-31").expect("date"),
        ]
    );
}

#[tokio::test]
async fn ensure_sessions_requires_a_bars_interval() {
    let environ = environment();
    let runtime = runtime(&environ);
    let bars = lake_coverage::EnsureSpec {
        data_type: lake_contracts::DataType::Bars,
        interval: None,
        ..equity_spec("2026-08-28", "2026-08-28")
    };

    let error = runtime
        .ensure_sessions(&bars)
        .await
        .expect_err("bars without an interval");
    assert!(error.to_string().contains("bars requires an interval"));
}

#[tokio::test]
async fn ensure_sessions_requires_provider_credentials() {
    let mut environ = environment();
    environ.remove("LAKE_PROVIDER__API_KEY");
    environ.remove("LAKE_PROVIDER__API_SECRET");
    let runtime = runtime(&environ);

    let error = runtime
        .ensure_sessions(&equity_spec("2026-08-28", "2026-08-28"))
        .await
        .expect_err("must require credentials");
    assert!(error.to_string().contains("LAKE_PROVIDER__API_KEY"));
}

#[tokio::test]
async fn non_equity_ensure_requests_are_rejected() {
    let environ = environment();
    let runtime = runtime(&environ);

    let error = runtime
        .ensure_sessions(&crypto_spec())
        .await
        .expect_err("must reject non-equity");
    assert!(error.to_string().contains("equity"));
}

#[tokio::test]
async fn job_launcher_marks_the_durable_job_succeeded() {
    let environ = environment();
    let base = runtime(&environ);
    let jobs = Arc::new(lake_catalog::MemoryJobRepository::new());
    let base = base.with_jobs(Arc::clone(&jobs) as Arc<dyn lake_catalog::JobRepository>);
    let runtime = Arc::new(
        base.with_ingest_override(Arc::new(|_symbol: String, _sessions| {
            Box::pin(async { Ok(()) }) as BoxFuture<'static, Result<(), HostedEnsureError>>
        })),
    );

    let launch = runtime.job_launcher();
    let spec = lake_coverage::EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: lake_contracts::DataType::Trade,
        symbol: "AAPL".to_string(),
        start_date: Date::parse("2026-08-28").expect("date"),
        end_date: Date::parse("2026-08-28").expect("date"),
        interval: None,
    };
    launch(lake_coverage::EnsureJob {
        job_id: "ensure-test".to_string(),
        request: spec,
        gaps: Vec::new(),
    })
    .await
    .expect("launch");

    let record = jobs.get("ensure-test").expect("record").expect("stored");
    assert_eq!(record.state, lake_catalog::JobState::Succeeded);
}

#[tokio::test]
async fn job_launcher_propagates_ingest_failures() {
    let environ = environment();
    let base = runtime(&environ);
    let jobs = Arc::new(lake_catalog::MemoryJobRepository::new());
    let base = base.with_jobs(Arc::clone(&jobs) as Arc<dyn lake_catalog::JobRepository>);
    let runtime = Arc::new(base.with_ingest_override(Arc::new(
        |_symbol: String, _sessions: Vec<lake_coverage::CoverageSession>| {
            Box::pin(async { Err(HostedEnsureError::new("provider unavailable")) })
                as BoxFuture<'static, Result<(), HostedEnsureError>>
        },
    )));

    let launch = runtime.job_launcher();
    let spec = lake_coverage::EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: lake_contracts::DataType::Trade,
        symbol: "AAPL".to_string(),
        start_date: Date::parse("2026-08-28").expect("date"),
        end_date: Date::parse("2026-08-28").expect("date"),
        interval: None,
    };
    let error = launch(lake_coverage::EnsureJob {
        job_id: "ensure-test".to_string(),
        request: spec,
        gaps: Vec::new(),
    })
    .await
    .expect_err("must propagate");

    assert!(error.to_string().contains("provider unavailable"));
    assert_eq!(jobs.get("ensure-test").expect("get"), None);
}

#[test]
fn missing_storage_account_is_a_hosted_ensure_error() {
    let environ = BTreeMap::new();
    let error = HostedRuntime::from_environment(&environ, None).expect_err("must fail");
    assert!(error
        .to_string()
        .contains("LAKE_STORAGE_ACCOUNT_NAME is required"));
}

/// A committer that must never be reached in these tests.
#[derive(Default)]
struct NoopCommitter;

impl lake_ingestion::AtomicIngestCommitter for NoopCommitter {
    fn commit(
        &self,
        _objects: &[lake_ingestion::StagedParquetObject],
        _coverage: &[lake_catalog::CoverageRecord],
    ) -> Result<(), lake_contracts::DataError> {
        panic!("the option transaction must not commit in this test");
    }
}

/// A provider client that must never be reached in these tests.
struct UnreachableMarketClient;

impl lake_ingestion::OptionMarketDataClient for UnreachableMarketClient {
    fn get_option_trades<'a>(
        &'a self,
        _contracts: &'a [lake_catalog::ContractRecord],
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<
        'a,
        Result<Vec<lake_providers::NormalizedOptionTrade>, lake_providers::ProviderError>,
    > {
        Box::pin(async {
            Err(lake_providers::ProviderError::Configuration(
                "unreachable".to_string(),
            ))
        })
    }

    fn get_option_quotes<'a>(
        &'a self,
        _contracts: &'a [lake_catalog::ContractRecord],
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<
        'a,
        Result<Vec<lake_providers::NormalizedOptionQuote>, lake_providers::ProviderError>,
    > {
        Box::pin(async {
            Err(lake_providers::ProviderError::Configuration(
                "unreachable".to_string(),
            ))
        })
    }
}

#[tokio::test]
async fn option_queue_handler_adapter_maps_payloads_and_fails_closed() {
    let transaction = Arc::new(lake_ingestion::OptionIngestTransaction::new(
        Arc::new(UnreachableMarketClient),
        Arc::new(lake_ingestion::StagingPartitionWriter::new()),
        Arc::new(NoopCommitter),
    ));
    let handler = lake_hosted::OptionQueueHandlerAdapter::new(transaction, None);

    // A non-option payload fails validation before any provider call.
    let delivery = delivery(r#"{"asset_class":"equity","symbol":"AAPL"}"#);
    let error = lake_queue::QueueHandler::handle(&handler, &delivery)
        .await
        .expect_err("must reject");
    assert!(error.to_string().contains("asset_class"));
}

#[tokio::test]
async fn composed_queue_worker_uses_the_settings_policy() {
    let settings = test_queue_settings();
    let policy = lake_hosted::queue_worker_policy(&settings).expect("policy");
    assert_eq!(policy.visibility_timeout_seconds, 300);
    assert_eq!(policy.visibility_renewal_seconds, 60);
    assert_eq!(policy.max_delivery_attempts, 5);
    assert!(policy.dead_letter_on_exhaustion);

    let transport = Arc::new(RecordingQueueClient::default());
    let worker = compose_worker(transport, &settings);
    // Receiving on an empty fake queue yields no delivery.
    let processed = worker.process_one().await.expect("process");
    assert!(processed.is_none());
}

fn test_queue_settings() -> lake_configuration::QueueSettings {
    lake_configuration::QueueSettings {
        visibility_timeout_seconds: 300,
        visibility_renewal_seconds: 60,
        max_delivery_attempts: 5,
        dead_letter_on_exhaustion: true,
    }
}

fn compose_worker(
    queue: Arc<dyn lake_queue::AzureQueueClient>,
    settings: &lake_configuration::QueueSettings,
) -> lake_queue::QueueWorker {
    let transaction = Arc::new(lake_ingestion::OptionIngestTransaction::new(
        Arc::new(UnreachableMarketClient),
        Arc::new(lake_ingestion::StagingPartitionWriter::new()),
        Arc::new(NoopCommitter),
    ));
    let sender: Arc<dyn lake_storage::QueueSender> = Arc::new(lake_queue::AzureQueueSender::new(
        Arc::new(RecordingQueueClient::default()),
    ));
    lake_hosted::compose_option_queue_worker(
        queue,
        transaction,
        None,
        settings,
        sender,
        "options-dead-letter",
        1.0,
    )
    .expect("worker")
}

/// A queue client fake that never yields a message.
#[derive(Default)]
struct RecordingQueueClient {
    received: Mutex<Vec<u64>>,
}

impl lake_queue::AzureQueueClient for RecordingQueueClient {
    fn receive_one<'s>(
        &'s self,
        visibility_timeout_seconds: u64,
    ) -> BoxFuture<
        's,
        Result<Option<lake_queue::adapters::AzureRawMessage>, lake_contracts::DataError>,
    > {
        self.received
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(visibility_timeout_seconds);
        Box::pin(async { Ok(None) })
    }

    fn update<'s>(
        &'s self,
        _message_id: &'s str,
        _pop_receipt: &'s str,
        _content: &'s str,
        _visibility_timeout_seconds: u64,
    ) -> BoxFuture<'s, Result<String, lake_contracts::DataError>> {
        Box::pin(async { Ok("receipt".to_string()) })
    }

    fn delete<'s>(
        &'s self,
        _message_id: &'s str,
        _pop_receipt: &'s str,
    ) -> BoxFuture<'s, Result<(), lake_contracts::DataError>> {
        Box::pin(async { Ok(()) })
    }

    fn send<'s>(
        &'s self,
        _content: String,
    ) -> BoxFuture<'s, Result<(), lake_contracts::DataError>> {
        Box::pin(async { Ok(()) })
    }
}

/// Build one delivery like the worker would decode it.
fn delivery(content: &str) -> lake_queue::QueueDelivery {
    let message = lake_queue::QueueMessage::new("m-1", "r-1", content, 1).expect("message");
    let payload: serde_json::Value = serde_json::from_str(content).expect("payload");
    lake_queue::QueueDelivery::from_parts(message, payload)
}

/// The published API keeps `/health` open in both compositions.
#[tokio::test]
async fn api_router_health_is_public_in_both_compositions() {
    let environ = environment();
    let now: lake_http::ApiClock =
        Arc::new(|| DateTime::from_ymd_hms(2026, 9, 4, 12, 0, 0, 0).expect("clock"));
    let router = lake_hosted::api::create_api_router(&environ, Arc::clone(&now)).expect("router");
    let response = send_health(&router).await;
    assert_eq!(response, 200);

    let empty = BTreeMap::new();
    let router = lake_hosted::api::create_api_router(&empty, now).expect("router");
    let response = send_health(&router).await;
    assert_eq!(response, 200);
}

async fn send_health(router: &axum::Router) -> axum::http::StatusCode {
    use tower::ServiceExt;
    let request = axum::http::Request::builder()
        .method("GET")
        .uri("/health")
        .body(axum::body::Body::empty())
        .expect("request");
    let response = router.clone().oneshot(request).await.expect("oneshot");
    response.status()
}
