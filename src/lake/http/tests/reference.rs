//! Point-in-time reference universe and facts route behavior.

mod support;

use std::sync::Arc;

use axum::http::StatusCode;
use lake_catalog::CatalogRepository;
use lake_contracts::Date;
use support::*;

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn ymd(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).expect("valid date")
}

fn seeded_store() -> (tempfile::TempDir, Arc<lake_storage::LocalFilesystemStore>) {
    let temporary = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(lake_storage::LocalFilesystemStore::new(temporary.path()));
    write_classification_snapshot(
        &store,
        date(4, 18),
        &[
            SnapshotRow {
                symbol: "AAPL",
                security_name: "Apple Inc. Common Stock",
                listing_exchange: "q",
                market_category: "Q",
                etf: false,
                round_lot_size: "100",
                financial_status: Some("Q"),
                cik: Some(320193),
                sic: Some("3571"),
                sic_industry: Some("ELECTRONIC COMPUTERS"),
            },
            SnapshotRow {
                symbol: "SPY",
                security_name: "SPDR S&P 500 ETF TRUST",
                listing_exchange: "P",
                market_category: "P",
                etf: true,
                round_lot_size: "100",
                financial_status: None,
                cik: Some(884394),
                sic: None,
                sic_industry: None,
            },
            SnapshotRow {
                symbol: "MSFT",
                security_name: "Microsoft Corporation Common Stock",
                listing_exchange: "Q",
                market_category: "Q",
                etf: false,
                round_lot_size: "100",
                financial_status: None,
                cik: None,
                sic: None,
                sic_industry: None,
            },
        ],
    );
    write_classification_snapshot(
        &store,
        date(4, 19),
        &[
            SnapshotRow {
                symbol: "AAPL",
                security_name: "Apple Inc. Common Stock",
                listing_exchange: "Q",
                market_category: "Q",
                etf: false,
                round_lot_size: "100",
                financial_status: Some("Q"),
                cik: Some(320193),
                sic: Some("3571"),
                sic_industry: Some("ELECTRONIC COMPUTERS"),
            },
            SnapshotRow {
                symbol: "TSLA",
                security_name: "Tesla Inc. Common Stock",
                listing_exchange: "Q",
                market_category: "Q",
                etf: false,
                round_lot_size: "100",
                financial_status: None,
                cik: Some(1318605),
                sic: Some("3711"),
                sic_industry: Some("MOTOR VEHICLES & PASSENGER CAR BODIES"),
            },
        ],
    );
    write_fundamentals(
        &store,
        vec![
            fact(
                320193,
                "dei",
                "EntityCommonStockSharesOutstanding",
                "shares",
                ymd(2023, 10, 20),
                ymd(2023, 11, 3),
                "0000320193-23-000106",
                "15464661000",
            ),
            fact(
                320193,
                "us-gaap",
                "Revenues",
                "USD",
                ymd(2023, 12, 30),
                ymd(2024, 2, 2),
                "0000320193-24-000006",
                "119575000000",
            ),
            fact(
                320193,
                "us-gaap",
                "NetIncomeLoss",
                "USD",
                ymd(2023, 12, 30),
                ymd(2024, 2, 2),
                "0000320193-24-000006",
                "33916000000",
            ),
            fact(
                320193,
                "us-gaap",
                "EarningsPerShareDiluted",
                "USD/shares",
                ymd(2023, 12, 30),
                ymd(2024, 2, 2),
                "0000320193-24-000006",
                "6.13",
            ),
            fact(
                320193,
                "us-gaap",
                "Revenues",
                "USD",
                ymd(2024, 3, 30),
                ymd(2024, 5, 3),
                "0000320193-24-000053",
                "120000000000",
            ),
        ],
    );
    (temporary, store)
}

fn reference_router(store: &Arc<lake_storage::LocalFilesystemStore>) -> axum::Router {
    let catalog = shared_catalog([]);
    let mut state = base_state(Arc::clone(&catalog) as Arc<dyn CatalogRepository>);
    state.read_store = Some(Arc::clone(store) as Arc<dyn lake_storage::ReadableBlobStore>);
    build_app(state)
}

#[tokio::test]
async fn universe_is_point_in_time_canonical_and_never_errors_for_a_missing_snapshot() {
    let (_temporary, store) = seeded_store();
    let router = reference_router(&store);

    let (status, _, body) =
        send(&router, get_auth("/v1/reference/universe?as_of=2024-04-18")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["as_of"], "2024-04-18");
    assert_eq!(body["symbols"], serde_json::json!(["AAPL", "MSFT", "SPY"]));

    let (status, _, body) =
        send(&router, get_auth("/v1/reference/universe?as_of=2024-04-19")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["symbols"], serde_json::json!(["AAPL", "TSLA"]));

    let (status, _, body) =
        send(&router, get_auth("/v1/reference/universe?as_of=2024-04-17")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["symbols"], serde_json::json!([]));
}

#[tokio::test]
async fn facts_join_classification_and_fundamentals_point_in_time() {
    let (_temporary, store) = seeded_store();
    let router = reference_router(&store);

    let (status, _, body) = send(
        &router,
        get_auth("/v1/reference/facts?as_of=2024-04-18&symbols=aapl,aaa,msft,spy"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["as_of"], "2024-04-18");
    let aapl = &body["facts"]["AAPL"];
    assert_eq!(aapl["security_type"], "stock");
    assert_eq!(aapl["exchange"], "Q");
    assert_eq!(aapl["security_name"], "Apple Inc. Common Stock");
    assert_eq!(aapl["market_category"], "Q");
    assert_eq!(aapl["round_lot_size"], "100");
    assert_eq!(aapl["sic_code"], "3571");
    assert_eq!(aapl["sic_industry"], "ELECTRONIC COMPUTERS");
    assert_eq!(aapl["shares_outstanding"], "15464661000", "{body}");
    assert_eq!(aapl["shares_outstanding_filed"], "2023-11-03");
    assert_eq!(aapl["revenue"], "119575000000");
    assert_eq!(aapl["revenue_filed"], "2024-02-02");
    assert_eq!(aapl["net_income"], "33916000000");
    assert_eq!(aapl["net_income_filed"], "2024-02-02");
    assert_eq!(aapl["eps"], "6.13");
    assert_eq!(aapl["eps_filed"], "2024-02-02");

    assert_eq!(body["facts"]["AAA"], serde_json::json!({}));
    assert_eq!(body["facts"]["MSFT"]["security_type"], "stock");
    assert!(body["facts"]["MSFT"].get("shares_outstanding").is_none());
    assert_eq!(body["facts"]["SPY"]["security_type"], "etf");
    assert!(body["facts"]["SPY"].get("revenue").is_none());
}

#[tokio::test]
async fn facts_restatements_are_not_visible_before_their_filing_date() {
    let (_temporary, store) = seeded_store();
    let router = reference_router(&store);

    let (status, _, body) = send(
        &router,
        get_auth("/v1/reference/facts?as_of=2024-04-19&symbols=AAPL"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["facts"]["AAPL"]["revenue"], "119575000000");
    assert_eq!(body["facts"]["AAPL"]["revenue_filed"], "2024-02-02");

    let (status, _, body) = send(
        &router,
        get_auth("/v1/reference/facts?as_of=2024-05-04&symbols=AAPL"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["facts"]["AAPL"]["revenue"], "120000000000");
    assert_eq!(body["facts"]["AAPL"]["revenue_filed"], "2024-05-03");
}

#[tokio::test]
async fn reference_routes_require_authentication() {
    let (_temporary, store) = seeded_store();
    let router = reference_router(&store);

    let (status, _, _) = send(&router, get("/v1/reference/universe?as_of=2024-04-18")).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _, _) = send(
        &router,
        get("/v1/reference/facts?as_of=2024-04-18&symbols=AAPL"),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}
