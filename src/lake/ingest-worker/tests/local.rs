//! Finite real worker + filesystem persistence + injected provider HTTP; no Azure or provider account.
use lake_catalog::CatalogRepository;
use lake_contracts::{AssetClass, DataType, Date};
use lake_ingest_jobs::{
    IngestionJobs, JobOwnership, JobPersistence, LocalConfig, WorkRequest, WorkerRunner,
};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

#[derive(Default)]
struct ProviderConcurrency {
    active_kinds: Mutex<BTreeMap<String, usize>>,
    parallel_partitions: AtomicBool,
    changed: tokio::sync::Notify,
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn local_worker_publishes_all_datasets_and_retained_admission_survives_host_restart() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let provider = format!("http://{}", listener.local_addr().unwrap());
    let concurrency = Arc::new(ProviderConcurrency::default());
    let fake=axum::Router::new().route("/{*path}",axum::routing::get(|
        axum::extract::State(concurrency): axum::extract::State<Arc<ProviderConcurrency>>,
        axum::extract::Path(path):axum::extract::Path<String>,
        axum::extract::Query(params):axum::extract::Query<BTreeMap<String,String>>
    |async move{
        let kind=path.rsplit('/').next().unwrap();
        let entered = concurrency.changed.notified();
        {
            let mut active = concurrency.active_kinds.lock().unwrap();
            *active.entry(kind.into()).or_default() += 1;
            if active.len() >= 2 {
                concurrency.parallel_partitions.store(true, Ordering::SeqCst);
                concurrency.changed.notify_waiters();
            }
        }
        if !concurrency.parallel_partitions.load(Ordering::SeqCst) {
            let _ = tokio::time::timeout(Duration::from_secs(2), entered).await;
        }
        let time=params.get("start").unwrap();
        let row=if kind=="trades"{serde_json::json!({"t":time,"p":100.5,"s":100,"x":"V","c":["@"],"i":1,"z":"C"})}else if kind=="quotes"{serde_json::json!({"t":time,"bp":100,"bs":100,"bx":"V","ap":101,"as":100,"ax":"V","c":[],"z":"C"})}else{serde_json::json!({"t":time,"o":100,"h":101,"l":99,"c":100.5,"v":100,"n":1,"vw":100.5})};
        let symbol = params.get("symbols").map(String::as_str).unwrap_or("AAPL");
        let rows=if path.contains("/AAPL/"){serde_json::json!([row])}else{serde_json::json!({symbol:[row]})};
        let mut body=serde_json::Map::new();body.insert(kind.into(),rows);body.insert("next_page_token".into(),serde_json::Value::Null);
        {
            let mut active = concurrency.active_kinds.lock().unwrap();
            let count = active.get_mut(kind).unwrap();
            *count -= 1;
            if *count == 0 { active.remove(kind); }
        }
        axum::Json(serde_json::Value::Object(body))
    })).with_state(concurrency.clone());
    let server = tokio::spawn(async move { axum::serve(listener, fake).await.unwrap() });
    let dir = tempfile::tempdir().unwrap();
    let env_file = dir.path().join("provider.env");
    std::fs::write(&env_file,format!("LAKE_PROVIDER__API_KEY=fake\nLAKE_PROVIDER__API_SECRET=fake\nLAKE_PROVIDER__BASE_URL={provider}\nLAKE_PROVIDER__REQUEST__MAX_CONCURRENCY=8\nLAKE_PROVIDER__REQUEST__REQUESTS_PER_MINUTE=10000\n")).unwrap();
    let layout = lake_local::LocalLayout::new(dir.path().join("data"));
    layout.initialize().unwrap();
    let configuration = LocalConfig {
        host_memory_mib: 8192,
        executable: env!("CARGO_BIN_EXE_lake-ingest-worker").into(),
        provider_env: Some(env_file),
        root: layout.root().into(),
        max_workers: 2,
        timeout_seconds: 60,
        partition_days: 1,
        session_concurrency: 2,
    };
    let store = lake_local::LocalJobStore::new(layout.root().into());
    let controller = IngestionJobs {
        store: store.clone(),
        runner: lake_local::LocalRunner::new(configuration.clone()).unwrap(),
    };
    let work: Vec<_> = ["trade", "quote", "bars"]
        .into_iter()
        .map(|data| WorkRequest::Dataset {
            asset_class: "equity".into(),
            data_type: data.into(),
            symbol: "AAPL".into(),
            start_date: "2024-01-18".into(),
            end_date: "2024-01-18".into(),
            interval: (data == "bars").then(|| "1min".into()),
        })
        .collect();
    let (first, second) = tokio::join!(
        controller.submit("local-worker".into(), work.clone()),
        controller.submit("local-worker".into(), work.clone())
    );
    assert_eq!(first.unwrap().record.job_id, second.unwrap().record.job_id);
    controller.reconcile().await.unwrap();
    let partitions = store
        .get("local-worker")
        .await
        .unwrap()
        .partitions()
        .unwrap();
    assert_eq!(partitions.len(), 3);
    assert!(partitions
        .iter()
        .all(|partition| partition.record.job_id == "local-worker"));
    let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
    loop {
        controller.reconcile().await.unwrap();
        let job = store.get("local-worker").await.unwrap();
        if job.terminal() {
            assert_eq!(
                job.record.state,
                lake_catalog::JobState::Complete,
                "{:?}",
                job.record.error_message
            );
            if job.execution_deleted {
                break;
            }
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "worker timeout; inspect {:?}",
            layout.root()
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(
        concurrency.parallel_partitions.load(Ordering::SeqCst),
        "independent finite worker processes must fetch different partitions concurrently"
    );
    for partition in &partitions {
        let reference = partition.reference();
        let persisted = store.get_work(&reference).await.unwrap();
        assert!(persisted.terminal());
        assert!(
            persisted.metrics.is_some(),
            "final counters must survive worker cleanup"
        );
        assert!(persisted.execution_deleted);
        assert!(!layout
            .root()
            .join("ingestion/scratch")
            .join(reference.worker_key().unwrap())
            .exists());
        let log = layout.root().join("ingestion/logs").join(format!(
            "{}.log",
            persisted
                .executor
                .as_ref()
                .expect("retained executor")
                .worker_key()
                .unwrap()
        ));
        let log = std::fs::read_to_string(log).unwrap();
        assert!(log.contains("worker_started"));
        let metrics = persisted
            .metrics
            .expect("final counters survive process cleanup");
        assert!(metrics["worker"]["seconds"].as_f64().unwrap() > 0.0);
        assert!(metrics["stages"]["network"]["bytes"].as_u64().unwrap() > 0);
        assert!(metrics["resources"]["cpu"]["peak"].as_u64().unwrap() > 0);
    }
    let catalog = Arc::new(lake_catalog::PublicationCatalog::new(
        Arc::new(lake_storage::LocalFilesystemStore::new(layout.objects())),
        Arc::new(lake_local::FileLeaseProvider(layout.objects())),
    ));
    for data in [DataType::Trade, DataType::Quote, DataType::Bars] {
        let row = catalog
            .get(
                AssetClass::Equity,
                data,
                (data == DataType::Bars).then_some("1min"),
                "AAPL",
                Date::parse("2024-01-18").unwrap(),
            )
            .unwrap()
            .unwrap();
        assert!(row.row_count > 0);
        assert!(row.checksum.is_some());
    }
    let restarted = IngestionJobs {
        store: store.clone(),
        runner: lake_local::LocalRunner::new(configuration.clone()).unwrap(),
    };
    assert_eq!(
        restarted
            .submit("local-worker".into(), work.clone())
            .await
            .unwrap()
            .record
            .state,
        lake_catalog::JobState::Complete
    );
    let mut changed = work;
    changed.pop();
    assert!(restarted
        .submit("local-worker".into(), changed)
        .await
        .is_err());
    let owner = store.own("local-worker").await.unwrap();
    assert!(store.own("local-worker").await.is_err());
    let mut forbidden = store.get("local-worker").await.unwrap();
    forbidden.record.state = lake_catalog::JobState::Running;
    assert!(owner.save(&forbidden).await.is_err());
    owner.release().await.unwrap();
    assert!(owner.renew().await.is_err());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}/market-data", listener.local_addr().unwrap());
    let env = BTreeMap::from([
        ("HARNESS_MARKET_DATA_MODE".into(), "local".into()),
        (
            "LAKE_LOCAL_DATA_DIR".into(),
            layout.root().display().to_string(),
        ),
        (
            "LAKE_LOCAL_WORKER_EXECUTABLE".into(),
            env!("CARGO_BIN_EXE_lake-ingest-worker").into(),
        ),
        (
            "LAKE_LOCAL_PROVIDER_ENV".into(),
            configuration
                .provider_env
                .as_ref()
                .unwrap()
                .display()
                .to_string(),
        ),
        ("LAKE_API__BEARER_TOKEN".into(), "local-test".into()),
        ("LAKE_LOCAL_PUBLIC_URL".into(), base.clone()),
    ]);
    let host = lake_hosted::api::create_host(
        &env,
        Arc::new(|| lake_contracts::DateTime::parse_iso_utc("2024-02-01T00:00:00Z").unwrap()),
        "http://invalid.test/internal-bind",
    )
    .unwrap();
    let access = host.access;
    let router = axum::Router::new().nest("/market-data", host.router);
    let api = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let uncached = || {
        harness_market_reader::MarketDataReader::new(harness_market_reader::CacheConfig {
            directory: dir.path().join("cache"),
            disk_bytes: 0,
            memory_bytes: 0,
        })
    };
    let direct = harness_lakeclient::LakeClient::from_access(access)
        .unwrap()
        .with_reader(uncached());
    let http = harness_lakeclient::LakeClient::new(
        harness_lakeclient::LakeClientConfig::new(&base, "local-test").unwrap(),
    )
    .unwrap()
    .with_reader(uncached());
    for data in ["trade", "quote", "bars"] {
        let request = harness_lakeclient::ReadSpec {
            asset_class: "equity".into(),
            data_type: data.into(),
            symbol: "AAPL".into(),
            start_date: "2024-01-18".into(),
            end_date: "2024-01-18".into(),
            interval: (data == "bars").then(|| "1min".into()),
        };
        let direct_rows = direct.read_arrow(&request).await.unwrap();
        let remote_rows = http.read_arrow(&request).await.unwrap();
        assert!(!direct_rows.is_empty());
        assert_eq!(direct_rows, remote_rows);
    }
    http.ensure(&harness_lakeclient::EnsureSpec::new(
        "crypto",
        "quote",
        "BTC/USD",
        "2024-01-18",
        "2024-01-18",
    ))
    .await
    .unwrap();
    let crypto = harness_lakeclient::ReadSpec {
        asset_class: "crypto".into(),
        data_type: "quote".into(),
        symbol: "BTC/USD".into(),
        start_date: "2024-01-18".into(),
        end_date: "2024-01-18".into(),
        interval: None,
    };
    let native_crypto = direct.read_arrow(&crypto).await.unwrap();
    let remote_crypto = http.read_arrow(&crypto).await.unwrap();
    assert!(!native_crypto.is_empty());
    assert_eq!(
        native_crypto, remote_crypto,
        "encoded crypto paths must survive signed HTTP transport"
    );
    let invalid_option = harness_lakeclient::EnsureSpec::new(
        "option",
        "quote",
        "AAPL240119C00100000",
        "2024-01-18",
        "2024-01-18",
    );
    for client in [&direct, &http] {
        assert!(
            client.admit_ensure(&invalid_option).await.is_err(),
            "unknown option contracts must fail before admission"
        );
    }
    let unsigned = reqwest::get(format!("{base}/v1/objects/date=2024-01-18/anything"))
        .await
        .unwrap();
    assert_eq!(unsigned.status().as_u16(), 400);
    http.ensure(&harness_lakeclient::EnsureSpec::new(
        "equity",
        "quote",
        "AAPL",
        "2024-01-19",
        "2024-01-19",
    ))
    .await
    .unwrap();
    assert!(
        catalog
            .get(
                AssetClass::Equity,
                DataType::Quote,
                None,
                "AAPL",
                Date::parse("2024-01-19").unwrap()
            )
            .unwrap()
            .unwrap()
            .row_count
            > 0
    );
    api.abort();
    for partition in partitions {
        controller
            .runner
            .delete(&partition.reference())
            .await
            .unwrap();
    }
    server.abort();
}
