//! Individual option resolution and all three datasets through the real local HTTP composition.
use axum::{
    body::Body,
    http::{Request, StatusCode},
    Router,
};
use futures_util::future::BoxFuture;
use http_body_util::BodyExt;
use lake_catalog::{ContractRecord, OptionType};
use lake_contracts::{AssetClass, BarInterval, Date, DateTime, Decimal18};
use lake_ingestion::{
    options::{resolve::OptionSelectionProvider, transaction::OptionMarketDataClient},
    BarsDataProvider,
};
use lake_local::{create_local_app_with, LocalLayout, LocalProviders};
use lake_providers::{
    NormalizedBar, NormalizedOptionQuote, NormalizedOptionTrade, NormalizedQuote, ProviderError,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};
use tower::ServiceExt;

const SYMBOL: &str = "SPY240216C00100000";
fn time(s: &str) -> DateTime {
    DateTime::parse_iso_utc(s).unwrap()
}
fn decimal(s: &str) -> Decimal18 {
    s.parse().unwrap()
}
fn contract(symbol: &str, strike: &str) -> ContractRecord {
    ContractRecord::new(
        symbol,
        "SPY",
        Date::parse("2024-02-16").unwrap(),
        decimal(strike),
        OptionType::Call,
        100,
        None,
        None,
    )
    .unwrap()
}
fn quote(c: &ContractRecord, at: DateTime, price: &str) -> NormalizedOptionQuote {
    NormalizedOptionQuote {
        timestamp: at,
        symbol: c.symbol.clone(),
        underlying: c.underlying.clone(),
        expiration_date: c.expiration_date,
        strike_price: c.strike_price,
        option_type: c.option_type,
        bid_exchange: "A".into(),
        ask_exchange: "A".into(),
        bid_price: decimal(price),
        ask_price: decimal(price),
        bid_size: 10,
        ask_size: 10,
        condition: "R".into(),
    }
}

#[derive(Default)]
struct Provider {
    dataset_calls: AtomicUsize,
    stale: bool,
    empty_bars: bool,
}
impl OptionSelectionProvider for Provider {
    fn contracts<'a>(
        &'a self,
        _: &'a str,
        _: Date,
        _: Date,
    ) -> BoxFuture<'a, Result<Vec<ContractRecord>, ProviderError>> {
        Box::pin(async {
            Ok(vec![
                contract(SYMBOL, "100"),
                contract("SPY240216C00110000", "110"),
            ])
        })
    }
    fn option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        _: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionQuote>, ProviderError>> {
        Box::pin(async move {
            let at = time(if self.stale {
                "2024-01-17T20:59:00Z"
            } else {
                "2024-01-18T20:59:00Z"
            });
            let mut rows: Vec<_> = contracts
                .iter()
                .map(|c| quote(c, at, if c.symbol == SYMBOL { "2.3" } else { "0.15" }))
                .collect();
            // A future quote closer to target must never influence selection.
            rows.push(quote(
                &contract("SPY240216C00110000", "110"),
                time("2024-01-19T15:00:00Z"),
                "10",
            ));
            Ok(rows)
        })
    }
    fn underlying_quotes<'a>(
        &'a self,
        _: &'a str,
        _: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async {
            Ok(vec![NormalizedQuote {
                timestamp: time("2024-01-18T20:58:00Z"),
                symbol: "SPY".into(),
                bid_exchange: "N".into(),
                ask_exchange: "N".into(),
                bid_price: decimal("100"),
                ask_price: decimal("100"),
                bid_size: 10,
                ask_size: 10,
                conditions: vec![],
                tape: "C".into(),
            }])
        })
    }
}
impl OptionMarketDataClient for Provider {
    fn get_option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        _: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionQuote>, ProviderError>> {
        self.dataset_calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move {
            Ok(vec![quote(
                &contracts[0],
                time("2024-01-19T15:00:00Z"),
                "2.4",
            )])
        })
    }
    fn get_option_trades<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        _: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionTrade>, ProviderError>> {
        self.dataset_calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move {
            let c = &contracts[0];
            Ok(vec![NormalizedOptionTrade {
                timestamp: time("2024-01-19T15:00:00Z"),
                symbol: c.symbol.clone(),
                underlying: c.underlying.clone(),
                expiration_date: c.expiration_date,
                strike_price: c.strike_price,
                option_type: c.option_type,
                exchange: "A".into(),
                price: decimal("2.4"),
                size: 3,
                condition: "R".into(),
            }])
        })
    }
}
impl BarsDataProvider for Provider {
    fn fetch_bars<'a>(
        &'a self,
        asset: AssetClass,
        symbol: &'a str,
        _: BarInterval,
        _: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        assert_eq!(asset, AssetClass::Option);
        self.dataset_calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move {
            if self.empty_bars {
                return Ok(vec![]);
            }
            Ok(vec![NormalizedBar {
                timestamp: time("2024-01-19T15:00:00Z"),
                symbol: symbol.into(),
                open: decimal("2.4"),
                high: decimal("2.5"),
                low: decimal("2.3"),
                close: decimal("2.45"),
                volume: decimal("7"),
            }])
        })
    }
}
fn app(layout: &LocalLayout, provider: Arc<Provider>) -> Router {
    let env = BTreeMap::from([
        ("LAKE_API__BEARER_TOKEN".into(), "test".into()),
        ("LAKE_PROVIDER__API_KEY".into(), "test".into()),
        ("LAKE_PROVIDER__API_SECRET".into(), "test".into()),
    ]);
    create_local_app_with(
        layout,
        &env,
        Arc::new(|| time("2024-01-20T00:00:00Z")),
        LocalProviders {
            options: Some(provider.clone()),
            option_selection: Some(provider.clone()),
            bars: Some(provider),
            ..Default::default()
        },
    )
    .unwrap()
}
async fn send(app: &Router, path: &str, body: Option<Value>) -> (StatusCode, Vec<u8>) {
    let mut request = Request::builder()
        .uri(path)
        .header("authorization", "Bearer test");
    let body = if let Some(body) = body {
        request = request
            .method("POST")
            .header("content-type", "application/json");
        Body::from(body.to_string())
    } else {
        Body::empty()
    };
    let response = app
        .clone()
        .oneshot(request.body(body).unwrap())
        .await
        .unwrap();
    (
        response.status(),
        response
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .to_vec(),
    )
}
fn selector(delta: bool) -> Value {
    let mut s = json!({"underlying":"SPY","right":"call","as_of":"2024-01-18T21:00:00Z"});
    if delta {
        s["days_to_expiration"] = json!(30);
        s["delta"] = json!("0.50");
    } else {
        s["expiration"] = json!("2024-02-16");
        s["strike"] = json!("100");
    }
    s
}

#[tokio::test]
async fn strike_and_delta_select_the_same_contract_and_all_streams_survive_restart() {
    let dir = tempfile::tempdir().unwrap();
    let layout = LocalLayout::new(dir.path().join("lake"));
    let provider = Arc::new(Provider::default());
    let router = app(&layout, provider.clone());
    for delta in [false, true] {
        let (status, body) = send(&router, "/v1/options/resolve", Some(selector(delta))).await;
        assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
        let value: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(value["symbol"], SYMBOL);
        assert_eq!(value["multiplier"], 100);
        if delta {
            assert_eq!(value["method"], "quote_implied_black_scholes_v1");
            assert!(value["delta"].as_str().unwrap().parse::<f64>().unwrap() > 0.5);
        }
    }
    for (kind, interval) in [
        ("quote", None),
        ("trade", None),
        ("bars", Some("1min")),
        ("bars", Some("1d")),
    ] {
        let mut body = json!({"asset_class":"option","data_type":kind,"symbol":SYMBOL,"start_date":"2024-01-19","end_date":"2024-01-19"});
        let suffix = interval
            .map(|i| format!("&interval={i}"))
            .unwrap_or_default();
        if let Some(i) = interval {
            body["interval"] = json!(i);
        }
        let (status, bytes) = send(&router, "/v1/catalog/ensure", Some(body.clone())).await;
        assert_eq!(
            status,
            StatusCode::OK,
            "{}",
            String::from_utf8_lossy(&bytes)
        );
        let path=format!("/v1/read?asset_class=option&data_type={kind}&symbol={SYMBOL}&start_date=2024-01-19&end_date=2024-01-19{suffix}");
        let (status, bytes) = send(&router, &path, None).await;
        assert_eq!(status, StatusCode::OK);
        let batches =
            arrow_ipc::reader::StreamReader::try_new(std::io::Cursor::new(bytes), None).unwrap();
        assert_eq!(batches.map(|b| b.unwrap().num_rows()).sum::<usize>(), 1);
        let mut query = body.clone();
        query["mode"] = json!("file");
        let (status, bytes) = send(&router, "/v1/query", Some(query)).await;
        assert_eq!(
            status,
            StatusCode::OK,
            "{}",
            String::from_utf8_lossy(&bytes)
        );
        let files: Value = serde_json::from_slice(&bytes).unwrap();
        if let Some(i) = interval {
            assert_eq!(files["files"][0]["interval"], i);
        }
        let restarted = app(&layout, provider.clone());
        assert_eq!(
            send(&restarted, "/v1/catalog/ensure", Some(body)).await.0,
            StatusCode::OK
        );
        assert_eq!(send(&restarted, &path, None).await.0, StatusCode::OK);
    }
    assert_eq!(provider.dataset_calls.load(Ordering::SeqCst), 4);
}

#[tokio::test]
async fn missing_historical_quotes_and_future_or_ambiguous_selectors_fail_closed() {
    let dir = tempfile::tempdir().unwrap();
    let layout = LocalLayout::new(dir.path().join("lake"));
    let router = app(
        &layout,
        Arc::new(Provider {
            stale: true,
            ..Default::default()
        }),
    );
    assert_eq!(
        send(&router, "/v1/options/resolve", Some(selector(true)))
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    let mut future = selector(true);
    future["as_of"] = json!("2025-01-01T00:00:00Z");
    assert_eq!(
        send(&router, "/v1/options/resolve", Some(future)).await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    let mut ambiguous = selector(true);
    ambiguous["strike"] = json!("100");
    assert_eq!(
        send(&router, "/v1/options/resolve", Some(ambiguous))
            .await
            .0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    for at in [
        "2024-01-18T25:00:00Z",
        "2024-01-18T21:00:00",
        "2024-01-18T21:00:00+garbage",
    ] {
        let mut invalid = selector(true);
        invalid["as_of"] = json!(at);
        assert_eq!(
            send(&router, "/v1/options/resolve", Some(invalid)).await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
}

#[tokio::test]
async fn empty_option_bars_are_terminal_and_do_not_fetch_trades() {
    let dir = tempfile::tempdir().unwrap();
    let layout = LocalLayout::new(dir.path().join("lake"));
    let provider = Arc::new(Provider {
        empty_bars: true,
        ..Default::default()
    });
    let router = app(&layout, provider.clone());
    assert_eq!(
        send(&router, "/v1/options/resolve", Some(selector(false)))
            .await
            .0,
        StatusCode::OK
    );
    let body = json!({"asset_class":"option","data_type":"bars","interval":"1min","symbol":SYMBOL,"start_date":"2024-01-19","end_date":"2024-01-19"});
    let (status, bytes) = send(&router, "/v1/catalog/ensure", Some(body)).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&bytes)
    );
    let coverage: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(coverage["entries"][0]["status"], "empty");
    assert_eq!(provider.dataset_calls.load(Ordering::SeqCst), 1);
}
