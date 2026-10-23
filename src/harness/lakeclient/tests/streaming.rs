//! Execution reads reuse pinned manifests and verified Arrow data through the real HTTP adapter.
use harness_lakeclient::testing::bars_parquet;
use harness_lakeclient::testing::FakeBarRow;
use harness_lakeclient::testing::FakeLakeConfig;
use harness_lakeclient::testing::FakeLakeServer;
use harness_lakeclient::LakeClient;
use harness_lakeclient::LakeClientConfig;
use harness_lakeclient::ReadSpec;
use harness_market_reader::CacheConfig;
use harness_market_reader::MarketDataReader;
use std::sync::Arc;
use std::sync::Mutex;

#[tokio::test]
async fn repeated_execution_reads_pin_one_manifest_and_reuse_verified_buffers() {
    let server = FakeLakeServer::start(FakeLakeConfig {
        partition_bodies: vec![(
            200,
            bars_parquet("SPY", &[FakeBarRow::new(0, "10", "11", "9", "10", "1")]),
        )],
        ..FakeLakeConfig::new("test")
    });
    let directory = tempfile::tempdir().unwrap();
    let progress = Arc::new(Mutex::new(Vec::new()));
    let recorded = progress.clone();
    let client = LakeClient::new(LakeClientConfig::new(server.base_url(), "test").unwrap())
        .unwrap()
        .with_reader(MarketDataReader::new(CacheConfig {
            directory: directory.path().into(),
            disk_bytes: 1_000_000,
            memory_bytes: 1_000_000,
        }))
        .with_progress(Arc::new(move |value| {
            recorded.lock().unwrap().push(value);
            Ok(())
        }));
    let spec =
        ReadSpec::new("equity", "bars", "SPY", "2024-01-02", "2024-01-02").with_interval("1min");
    let other = client.clone();
    let (first, second) = tokio::join!(client.read_arrow(&spec), other.read_arrow(&spec));
    let first = first.unwrap();
    let second = second.unwrap();
    assert_eq!(first[0].num_rows(), 1);
    assert_eq!(
        first[0].column(0).to_data().buffers()[0].as_ptr(),
        second[0].column(0).to_data().buffers()[0].as_ptr()
    );
    let requests = server.requests();
    assert_eq!(
        requests.iter().filter(|r| r.path == "/v1/snapshot").count(),
        1
    );
    assert_eq!(
        requests
            .iter()
            .filter(|r| r.path.starts_with("/objects/"))
            .count(),
        1
    );
    assert!(requests
        .iter()
        .filter(|r| r.path.starts_with("/objects/"))
        .all(|r| r.authorization.is_none()));
    assert!(progress.lock().unwrap().last().unwrap().completed);
    client
        .clone()
        .with_progress(Arc::new(|_| Ok(())))
        .read_arrow(&spec)
        .await
        .unwrap();
    assert_eq!(
        server
            .requests()
            .iter()
            .filter(|r| r.path == "/v1/snapshot")
            .count(),
        2
    );
    assert_eq!(
        server
            .requests()
            .iter()
            .filter(|r| r.path.starts_with("/objects/"))
            .count(),
        1
    );
}
