use lake_contracts::{AssetClass, DataType, Date};
use serde_json::json;

use super::{FileQueryFile, FileQueryResponse, FileQueryResults};

fn jan_18() -> Date {
    Date::from_ymd(2024, 1, 18).expect("valid date")
}

fn sample_file() -> FileQueryFile {
    FileQueryFile {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: "AAPL".to_string(),
        session_date: jan_18(),
        object_path: "date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet".to_string(),
        url: "https://blob.test/aapl?read-only-expiring".to_string(),
        sas_url: Some("https://blob.test/aapl?read-only-expiring".to_string()),
        row_count: 12,
        byte_size: 3456,
        checksum: Some("checksum".to_string()),
        interval: None,
    }
}

#[test]
fn file_query_response_serializes_the_route_shape() {
    let response = FileQueryResponse {
        files: vec![sample_file()],
        results: FileQueryResults {
            requested_sessions: 1,
            returned_files: 1,
            complete_sessions: 1,
            pending_sessions: 0,
            state: "complete".to_string(),
        },
        limitations: vec!["Only complete catalog partitions are returned.".to_string()],
    };

    let value = serde_json::to_value(&response).expect("serialize response");
    assert_eq!(value["files"][0]["asset_class"], "equity");
    assert_eq!(value["files"][0]["data_type"], "trade");
    assert_eq!(value["files"][0]["symbol"], "AAPL");
    assert_eq!(value["files"][0]["session_date"], "2024-01-18");
    assert_eq!(
        value["files"][0]["object_path"],
        "date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet"
    );
    assert_eq!(value["files"][0]["url"], value["files"][0]["sas_url"]);
    assert_eq!(value["files"][0]["row_count"], 12);
    assert_eq!(value["files"][0]["byte_size"], 3456);
    assert_eq!(value["files"][0]["checksum"], "checksum");
    assert!(value["files"][0].get("interval").is_none());
    assert_eq!(
        value["results"],
        json!({
            "requested_sessions": 1,
            "returned_files": 1,
            "complete_sessions": 1,
            "pending_sessions": 0,
            "state": "complete",
        })
    );
    assert_eq!(
        value["limitations"],
        json!(["Only complete catalog partitions are returned."])
    );
}

#[test]
fn bars_files_serialize_the_interval_only_when_present() {
    let file = FileQueryFile {
        data_type: DataType::Bars,
        object_path: "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet"
            .to_string(),
        interval: Some("1min".to_string()),
        ..sample_file()
    };
    let value = serde_json::to_value(&file).expect("serialize file");
    assert_eq!(value["data_type"], "bars");
    assert_eq!(
        value["object_path"],
        "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet"
    );
    assert_eq!(value["interval"], "1min");

    let legacy = serde_json::to_value(sample_file()).expect("serialize file");
    assert!(legacy.get("interval").is_none());
}

#[test]
fn optional_fields_serialize_as_null() {
    let mut file = sample_file();
    file.sas_url = None;
    file.checksum = None;

    let value = serde_json::to_value(&file).expect("serialize file");
    assert!(value["sas_url"].is_null());
    assert!(value["checksum"].is_null());
}
