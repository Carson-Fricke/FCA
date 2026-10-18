//! Provider protocol behavior for individual-option acquisition.
use lake_contracts::{AssetClass, BarInterval, Date, DateTime};
use lake_providers::{
    AlpacaClient, AlpacaTransport, BoxFuture, ProviderConfig, RequestGovernor, TransportFailure,
    TransportResponse,
};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

type Request = (String, Vec<(String, String)>);

#[derive(Default)]
struct Transport {
    requests: Mutex<Vec<Request>>,
    repeat: bool,
}
impl AlpacaTransport for Transport {
    fn get<'a>(
        &'a self,
        url: &'a str,
        params: &'a [(String, String)],
        _: &'a [(String, String)],
        _: Arc<lake_pipeline::Reservation>,
    ) -> BoxFuture<'a, Result<TransportResponse, TransportFailure>> {
        Box::pin(async move {
            self.requests
                .lock()
                .unwrap()
                .push((url.into(), params.to_vec()));
            let param = |key| {
                params
                    .iter()
                    .find(|(k, _)| k == key)
                    .map(|(_, v)| v.as_str())
            };
            let body: Value = if url.ends_with("/contracts") {
                if param("status") == Some("inactive") {
                    json!({"option_contracts":[]})
                } else {
                    json!({"option_contracts":[{"symbol":"SPY240216C00100000","underlying_symbol":"SPY","expiration_date":"2024-02-16","strike_price":"100","type":"call","size":"100"}],
                    "next_page_token":if self.repeat || param("page_token").is_none() {Some("next")}else{None}})
                }
            } else {
                json!({"bars":{"SPY240216C00100000":[{"t":"2024-01-19T15:00:00Z","o":2.4,"h":2.5,"l":2.3,"c":2.45,"v":7}]},
                    "next_page_token":if self.repeat || param("page_token").is_none() {Some("next")}else{None}})
            };
            Ok(TransportResponse {
                allocation: None,
                status: 200,
                retry_after: None,
                body: serde_json::to_vec(&body).unwrap(),
            })
        })
    }
}
fn client(transport: Arc<Transport>) -> AlpacaClient {
    AlpacaClient::new(
        ProviderConfig::new("https://data.test", None, None),
        transport,
        RequestGovernor::new(None),
    )
    .unwrap()
}
#[tokio::test]
async fn contract_master_and_bars_page_on_the_correct_endpoints() {
    let transport = Arc::new(Transport::default());
    let client = client(transport.clone());
    let contracts = client
        .fetch_option_contracts(
            "https://trading.test",
            "SPY",
            Date::parse("2024-02-16").unwrap(),
            Date::parse("2024-02-16").unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(contracts.len(), 1);
    assert_eq!(contracts[0].multiplier, 100);
    let start = DateTime::parse_iso_utc("2024-01-19T14:30:00Z").unwrap();
    let bars = client
        .fetch_bars(
            AssetClass::Option,
            "SPY240216C00100000",
            BarInterval::OneMinute,
            start,
            DateTime::from_micros(start.micros() + 3_600_000_000),
        )
        .await
        .unwrap();
    assert_eq!(bars.len(), 2);
    assert_eq!(bars[0].close.to_string(), "2.45");
    let requests = transport.requests.lock().unwrap();
    assert_eq!(requests.len(), 5);
    assert!(requests[..3]
        .iter()
        .all(|(url, _)| url == "https://trading.test/v2/options/contracts"));
    assert!(requests[2]
        .1
        .contains(&("status".into(), "inactive".into())));
    for (url, params) in &requests[3..] {
        assert_eq!(url, "https://data.test/v1beta1/options/bars");
        assert!(params.contains(&("symbols".into(), "SPY240216C00100000".into())));
        assert!(params.contains(&("timeframe".into(), "1Min".into())));
        assert!(!params
            .iter()
            .any(|(key, _)| key == "adjustment" || key == "feed"));
    }
}
#[tokio::test]
async fn repeated_contract_and_bar_cursors_fail_instead_of_looping() {
    let client = client(Arc::new(Transport {
        repeat: true,
        ..Transport::default()
    }));
    let day = Date::parse("2024-02-16").unwrap();
    assert!(client
        .fetch_option_contracts("https://trading.test", "SPY", day, day)
        .await
        .is_err());
    let start = DateTime::parse_iso_utc("2024-01-19T14:30:00Z").unwrap();
    assert!(client
        .fetch_bars(
            AssetClass::Option,
            "SPY240216C00100000",
            BarInterval::OneMinute,
            start,
            DateTime::from_micros(start.micros() + 3_600_000_000)
        )
        .await
        .is_err());
}
