//! Published snapshot identity, access renewal and rejection of incomplete metadata.
mod support;
use axum::http::StatusCode;
use lake_contracts::{AssetClass, DataType, Date};
use serde_json::json;
use std::sync::Arc;
use support::*;
fn build_read_app(state: lake_http::LakeApiState) -> axum::Router {
    use sha2::{Digest, Sha256};
    if let Some(store) = &state.read_store {
        for mut record in state
            .catalog
            .list(&lake_catalog::CoverageQuery::default())
            .unwrap()
        {
            if let Some(path) = &record.object_path {
                if let Ok(bytes) = store.read(path) {
                    record.checksum = Some(hex::encode(Sha256::digest(&bytes)));
                    record.byte_size = bytes.len() as u64;
                    if let Ok(builder) =
                        parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(
                            axum::body::Bytes::from(bytes),
                        )
                    {
                        record.row_count = builder.metadata().file_metadata().num_rows() as u64;
                    }
                    state.catalog.upsert(&record).unwrap();
                }
            }
        }
    }
    build_app(state)
}

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

#[tokio::test]
async fn snapshots_pin_publication_metadata_and_renew_locations_without_changing_identity() {
    let temporary = tempfile::tempdir().unwrap();
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    write_market_partition(
        &store,
        AssetClass::Equity,
        DataType::Trade,
        None,
        "AAPL",
        date(1, 18),
        &[
            json!({"timestamp":"2024-01-18T14:30:00Z","trade_id":1,"exchange":"XNAS","price":"190","size":10,"conditions":["@"],"tape":"C"}),
        ],
    );
    let catalog = shared_catalog([complete_record(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        date(1, 18),
    )]);
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(catalog);
    state.read_store = Some(store);
    state.sas_issuer = Some(issuer.clone());
    let router = build_read_app(state);
    let response=send(&router,get_auth("/v1/snapshot?asset_class=equity&data_type=trade&symbol=aapl&start_date=2024-01-18&end_date=2024-01-18")).await;
    assert_eq!(response.0, StatusCode::OK, "{:?}", response.2);
    let snapshot = response.2["snapshot"].clone();
    assert_eq!(snapshot["request"]["symbol"], "AAPL");
    assert!(snapshot.get("locations").is_none());
    let renewed = send(
        &router,
        post_json("/v1/snapshot/locations", Some(TOKEN), snapshot.clone()),
    )
    .await;
    assert_eq!(renewed.0, StatusCode::OK);
    assert_eq!(renewed.2, response.2["locations"]);
    let mut forged = snapshot;
    forged["files"][0]["row_count"] = json!(999);
    assert_eq!(
        send(
            &router,
            post_json("/v1/snapshot/locations", Some(TOKEN), forged)
        )
        .await
        .0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert_eq!(issuer.paths().len(), 2);
}

#[tokio::test]
async fn missing_published_checksums_are_rejected_before_object_access() {
    let mut record = complete_record(AssetClass::Equity, DataType::Trade, "AAPL", date(1, 18));
    record.checksum = None;
    let issuer = Arc::new(RecordingIssuer::default());
    let mut state = base_state(shared_catalog([record]));
    state.sas_issuer = Some(issuer.clone());
    let response=send(&build_app(state),get_auth("/v1/snapshot?asset_class=equity&data_type=trade&symbol=AAPL&start_date=2024-01-18&end_date=2024-01-18")).await;
    assert_eq!(response.0, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(issuer.paths().is_empty());
}
