//! Bounded acquisition, external canonical merge, and publication through real ports.
use axum::{extract::Query, routing::get, Router};
use lake_catalog::{CatalogRepository, PublicationCatalog};
use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::CoverageSession;
use lake_ingestion::{EquityIngestTransaction, ObjectCatalogCommitter, StagingPartitionWriter};
use lake_providers::{AlpacaClient, ProviderConfig, RequestGovernor};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};

#[tokio::test]
async fn oversized_session_spills_and_tied_pages_publish_identical_canonical_bytes() {
    const TOTAL: usize = 32769;
    let mib = 1024 * 1024;
    let pipeline = lake_pipeline::Pipeline::initialize(lake_pipeline::Limits {
        cpu_slots: 2,
        response_bytes: 24 * mib,
        max_response_bytes: mib,
        session_bytes: mib,
        max_session_bytes: mib,
        publication_slots: 1,
        scratch_bytes: 32 * mib,
    })
    .unwrap();
    let page_size = Arc::new(AtomicUsize::new(256));
    let size = page_size.clone();
    let app=Router::new().route("/v2/stocks/trades",get(move |Query(params):Query<BTreeMap<String,String>>| {
        let size=size.clone();async move {
            let first=params.get("page_token").map(|s|s.parse::<usize>().unwrap()).unwrap_or(0);
            let end=(first+size.load(Ordering::SeqCst)).min(TOTAL);
            let rows=(first..end).map(|i|format!(r#"{{"t":"2025-12-30T14:30:00Z","i":{},"x":"Q","p":190.123456789012345678,"s":1,"c":["@","I"],"z":"C"}}"#,TOTAL-i)).collect::<Vec<_>>().join(",");
            let cursor=if end<TOTAL {format!("\"{end}\"")}else{"null".into()};
            format!(r#"{{"trades":{{"MSFT":[{rows}]}},"next_page_token":{cursor}}}"#)
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let client = Arc::new(
        AlpacaClient::with_reqwest(
            ProviderConfig::new(base, None, None),
            RequestGovernor::new(None),
        )
        .unwrap(),
    );
    let directory = tempfile::tempdir().unwrap();
    let objects = Arc::new(lake_storage::LocalFilesystemStore::new(directory.path()));
    let catalog = Arc::new(PublicationCatalog::new(
        objects.clone(),
        Arc::new(lake_local::FileLeaseProvider(directory.path().into())),
    ));
    let day = Date::parse("2025-12-30").unwrap();
    let at = DateTime::parse_iso_utc("2025-12-30T14:30:00Z").unwrap();
    let session = CoverageSession {
        session_date: day,
        open_time: at,
        close_time: at,
        is_partial: false,
    };
    let mut hashes = Vec::new();
    for size in [256, 512] {
        page_size.store(size, Ordering::SeqCst);
        let transaction = EquityIngestTransaction::new(
            client.clone(),
            Arc::new(StagingPartitionWriter::new()),
            Arc::new(ObjectCatalogCommitter::new(
                objects.clone(),
                catalog.clone(),
            )),
        );
        let result = transaction
            .run("MSFT", &session, Some(&[DataType::Trade]))
            .await
            .unwrap();
        assert_eq!(result.objects.len(), 1, "{:?}", result.coverage);
        assert_eq!(result.objects[0].rows, TOTAL as u64);
        hashes.push(result.objects[0].checksum.clone());
        let path = directory.path().join(&result.objects[0].path);
        let reader = parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(
            std::fs::File::open(path).unwrap(),
        )
        .unwrap()
        .build()
        .unwrap();
        let mut ids = Vec::new();
        for batch in reader {
            let batch = batch.unwrap();
            ids.extend(
                batch
                    .column_by_name("trade_id")
                    .unwrap()
                    .as_any()
                    .downcast_ref::<arrow_array::UInt64Array>()
                    .unwrap()
                    .values()
                    .iter()
                    .copied(),
            );
            let prices = batch
                .column_by_name("price")
                .unwrap()
                .as_any()
                .downcast_ref::<arrow_array::Decimal128Array>()
                .unwrap();
            assert_eq!(prices.value(0), 190123456789012345678);
        }
        assert_eq!(ids, (1..=TOTAL as u64).collect::<Vec<_>>());
        assert_eq!(
            catalog
                .get(AssetClass::Equity, DataType::Trade, None, "MSFT", day)
                .unwrap()
                .unwrap()
                .row_count,
            TOTAL as u64
        );
    }
    assert_eq!(
        hashes[0], hashes[1],
        "page layout must not change canonical storage"
    );
    let snapshot = pipeline.snapshot();
    assert!(snapshot["resources"]["scratch"]["peak"].as_u64().unwrap() > 0);
    for pool in [
        "sessions",
        "responses",
        "decoded",
        "scratch",
        "cpu",
        "publications",
    ] {
        assert_eq!(snapshot["resources"][pool]["active"], 0, "{pool}");
    }
    server.abort();
}
