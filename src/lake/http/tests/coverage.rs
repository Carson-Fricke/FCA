//! Catalog coverage route behavior: shapes, calendar bounds, options.

mod support;

use std::sync::Arc;

use axum::http::StatusCode;
use lake_catalog::{
    CoverageRecord, CoverageStatus, DailyFrontSet, MemoryCatalogRepository,
    MemoryContractRepository, MemoryDailyFrontSetRepository,
};
use lake_contracts::{partition_path, AssetClass, DataType, Date};
use serde_json::json;
use support::*;

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn coverage_record(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    session_date: Date,
    status: CoverageStatus,
) -> CoverageRecord {
    let complete = status == CoverageStatus::Complete;
    CoverageRecord::new(
        asset_class,
        data_type,
        symbol,
        session_date,
        status,
        complete.then(|| {
            partition_path(session_date, asset_class, data_type, symbol).expect("canonical path")
        }),
        if complete { 12 } else { 0 },
        if complete { 3456 } else { 0 },
        None,
        (status == CoverageStatus::Error).then(|| "source unavailable".to_string()),
    )
    .expect("valid record")
}

fn coverage_path(
    asset_class: &str,
    data_type: &str,
    symbol: &str,
    start_date: &str,
    end_date: &str,
) -> String {
    format!(
        "/v1/catalog/coverage?asset_class={asset_class}&data_type={data_type}&symbol={symbol}&start_date={start_date}&end_date={end_date}"
    )
}

#[tokio::test]
async fn equity_coverage_matches_winter_bounds_and_the_early_close() {
    let catalog = Arc::new(MemoryCatalogRepository::from_records([
        coverage_record(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            date(11, 27),
            CoverageStatus::Complete,
        ),
        coverage_record(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            date(11, 29),
            CoverageStatus::Empty,
        ),
        coverage_record(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            date(12, 2),
            CoverageStatus::Error,
        ),
    ]));
    let mut state = base_state(catalog);
    state.now = clock_at(
        lake_contracts::DateTime::from_ymd_hms(2024, 12, 3, 0, 0, 0, 0).expect("datetime"),
    );
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(&coverage_path(
            "equity",
            "trade",
            "aapl",
            "2024-11-27",
            "2024-12-02",
        )),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["asset_class"], "equity");
    assert_eq!(body["data_type"], "trade");
    assert_eq!(body["symbol"], "AAPL");
    assert_eq!(
        body["entries"],
        json!([
            {
                "session_date": "2024-11-27",
                "open_time": "2024-11-27T14:30:00Z",
                "close_time": "2024-11-27T21:00:00Z",
                "is_partial": false,
                "status": "complete",
                "object_path": "date=2024-11-27/asset_class=equity/data_type=trade/AAPL.parquet",
                "row_count": 12,
                "byte_size": 3456,
                "checksum": null,
                "error_message": null,
            },
            {
                "session_date": "2024-11-29",
                "open_time": "2024-11-29T14:30:00Z",
                "close_time": "2024-11-29T18:00:00Z",
                "is_partial": false,
                "status": "empty",
                "object_path": null,
                "row_count": 0,
                "byte_size": 0,
                "checksum": null,
                "error_message": null,
            },
            {
                "session_date": "2024-12-02",
                "open_time": "2024-12-02T14:30:00Z",
                "close_time": "2024-12-02T21:00:00Z",
                "is_partial": false,
                "status": "error",
                "object_path": null,
                "row_count": 0,
                "byte_size": 0,
                "checksum": null,
                "error_message": "source unavailable",
            },
        ])
    );
}

#[tokio::test]
async fn crypto_coverage_includes_every_utc_day_and_marks_the_open_day_partial() {
    let session_dates: Vec<Date> = (18..=22).map(|day| date(1, day)).collect();
    let catalog = Arc::new(MemoryCatalogRepository::from_records(
        session_dates.iter().map(|session_date| {
            coverage_record(
                AssetClass::Crypto,
                DataType::Quote,
                "BTC/USD",
                *session_date,
                CoverageStatus::Complete,
            )
        }),
    ));
    let router = build_app(base_state(catalog));

    let (status, _, body) = send(
        &router,
        get_auth(&coverage_path(
            "crypto",
            "quote",
            "btc%2Fusd",
            "2024-01-18",
            "2024-01-22",
        )),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["symbol"], "BTC/USD");
    assert_eq!(
        body["entries"][2]["object_path"],
        "date=2024-01-20/asset_class=crypto/data_type=quote/BTC%2FUSD.parquet"
    );
    assert_eq!(
        body["entries"]
            .as_array()
            .expect("entries")
            .iter()
            .map(|entry| entry["status"].clone())
            .collect::<Vec<_>>(),
        vec![
            json!("complete"),
            json!("complete"),
            json!("complete"),
            json!("complete"),
            json!("partial"),
        ]
    );
    assert_eq!(body["entries"][4]["is_partial"], true);
    assert_eq!(body["entries"][4]["open_time"], "2024-01-22T00:00:00Z");
    assert_eq!(body["entries"][4]["close_time"], "2024-01-23T00:00:00Z");
}

#[tokio::test]
async fn coverage_filters_catalog_rows_by_the_requested_partition() {
    let session_date = date(11, 27);
    let catalog = Arc::new(MemoryCatalogRepository::from_records([
        coverage_record(
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
            session_date,
            CoverageStatus::Complete,
        ),
        coverage_record(
            AssetClass::Equity,
            DataType::Quote,
            "AAPL",
            session_date,
            CoverageStatus::Complete,
        ),
        coverage_record(
            AssetClass::Equity,
            DataType::Trade,
            "MSFT",
            session_date,
            CoverageStatus::Complete,
        ),
    ]));
    let mut state = base_state(catalog);
    state.now = clock_at(
        lake_contracts::DateTime::from_ymd_hms(2024, 12, 3, 0, 0, 0, 0).expect("datetime"),
    );
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(&coverage_path(
            "equity",
            "trade",
            "AAPL",
            "2024-11-27",
            "2024-11-27",
        )),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["entries"].as_array().expect("entries").len(), 1);
    assert_eq!(
        body["entries"][0]["object_path"],
        "date=2024-11-27/asset_class=equity/data_type=trade/AAPL.parquet"
    );
}

#[tokio::test]
async fn coverage_requires_authentication() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(
        &router,
        get(&coverage_path(
            "equity",
            "trade",
            "AAPL",
            "2024-01-01",
            "2024-01-01",
        )),
    )
    .await;

    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(body["error"]["code"], "unauthorized");
}

#[tokio::test]
async fn option_coverage_returns_selected_contracts_and_osi_partition_metadata() {
    let contract_symbol = "SPY240119C00500000";
    let contract_record = contract(contract_symbol, "SPY", date(1, 19));
    let catalog = Arc::new(MemoryCatalogRepository::from_records([coverage_record(
        AssetClass::Option,
        DataType::Trade,
        contract_symbol,
        date(1, 19),
        CoverageStatus::Complete,
    )]));
    let mut state = base_state(catalog);
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract_record,
    ])));
    let router = build_app(state);

    let path = format!(
        "/v1/catalog/coverage?asset_class=option&data_type=trade&underlying=spy&contract_symbols={contract_symbol}&start_date=2024-01-19&end_date=2024-01-19"
    );
    let (status, _, body) = send(&router, get_auth(&path)).await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["asset_class"], "option");
    assert_eq!(body["underlying"], "SPY");
    assert_eq!(body["contracts"], json!([contract_symbol]));
    assert_eq!(body["entries"][0]["contract_symbol"], contract_symbol);
    assert_eq!(body["entries"][0]["status"], "complete");
    assert_eq!(
        body["entries"][0]["object_path"],
        "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet"
    );
}

#[tokio::test]
async fn option_underlying_coverage_uses_the_persisted_daily_front_set() {
    let contract_symbol = "SPY240119P00450000";
    let contract_record = contract(contract_symbol, "SPY", date(1, 19));
    let mut state = base_state(Arc::new(MemoryCatalogRepository::new()));
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract_record.clone(),
    ])));
    state.front_sets = Some(Arc::new(MemoryDailyFrontSetRepository::from_records([
        DailyFrontSet::new("SPY", date(1, 19), vec![contract_record]).expect("front set"),
    ])));
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(
            "/v1/catalog/coverage?asset_class=option&data_type=quote&underlying=spy&start_date=2024-01-19&end_date=2024-01-19",
        ),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["contracts"], json!([contract_symbol]));
    assert_eq!(body["entries"][0]["contract_symbol"], contract_symbol);
    assert_eq!(body["entries"][0]["status"], "missing");
}

#[tokio::test]
async fn option_coverage_accepts_repeated_and_alias_contract_selection() {
    let first_symbol = "SPY240119C00500000";
    let second_symbol = "SPY240119C00510000";
    let mut state = base_state(Arc::new(MemoryCatalogRepository::new()));
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract(first_symbol, "SPY", date(1, 19)),
        contract(second_symbol, "SPY", date(1, 19)),
    ])));
    let router = build_app(state);

    let repeated = send(
        &router,
        get_auth(&format!(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&contract_symbols={first_symbol}&contract_symbols={second_symbol}&start_date=2024-01-19&end_date=2024-01-19"
        )),
    )
    .await;
    let alias = send(
        &router,
        get_auth(&format!(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&contracts={second_symbol}&contracts={first_symbol}&start_date=2024-01-19&end_date=2024-01-19"
        )),
    )
    .await;

    assert_eq!(repeated.0, StatusCode::OK);
    assert_eq!(
        repeated.2["contracts"],
        json!([first_symbol, second_symbol])
    );
    assert_eq!(alias.0, StatusCode::OK);
    assert_eq!(alias.2["contracts"], json!([first_symbol, second_symbol]));
}

#[tokio::test]
async fn option_selection_reports_unknown_contracts_by_name() {
    let contract_symbol = "SPY240119C00500000";
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(
        &router,
        get_auth(&format!(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&contract_symbols={contract_symbol}&start_date=2024-01-19&end_date=2024-01-19"
        )),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "option_contract_not_found");
    assert_eq!(
        body["error"]["message"],
        format!("selected option contract does not exist: {contract_symbol}")
    );
}

#[tokio::test]
async fn option_underlying_without_metadata_reports_selection_unavailable() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(
        &router,
        get_auth(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&underlying=spy&start_date=2024-01-19&end_date=2024-01-19",
        ),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "option_selection_unavailable");
    assert_eq!(
        body["error"]["message"],
        "option contract metadata or a persisted front set is required"
    );
}

#[tokio::test]
async fn option_contracts_must_share_the_requested_underlying() {
    let contract_symbol = "SPY240119C00500000";
    let contract_record = contract(contract_symbol, "SPY", date(1, 19));
    let mut state = base_state(Arc::new(MemoryCatalogRepository::new()));
    state.contracts = Some(Arc::new(MemoryContractRepository::from_records([
        contract_record,
    ])));
    let router = build_app(state);

    let (status, _, body) = send(
        &router,
        get_auth(&format!(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&underlying=qqq&contract_symbols={contract_symbol}&start_date=2024-01-19&end_date=2024-01-19"
        )),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "option_underlying_mismatch");
    assert_eq!(
        body["error"]["message"],
        "selected option contracts must share the requested underlying"
    );
}

#[tokio::test]
async fn legacy_option_symbols_must_identify_an_osi_contract() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(
        &router,
        get_auth(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&symbol=aapl&start_date=2024-01-19&end_date=2024-01-19",
        ),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "unsupported_asset_class");
    assert_eq!(
        body["error"]["message"],
        "option symbol must identify an OSI contract"
    );
}

#[tokio::test]
async fn option_coverage_requires_a_selection() {
    let router = build_app(base_state(Arc::new(MemoryCatalogRepository::new())));

    let (status, _, body) = send(
        &router,
        get_auth(
            "/v1/catalog/coverage?asset_class=option&data_type=trade&start_date=2024-01-19&end_date=2024-01-19",
        ),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "validation_error");
}
