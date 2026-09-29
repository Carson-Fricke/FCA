//! Fake-transport behavior tests for the Alpaca client.
//!
//! The transport is injected, so these tests cover request shapes, paging,
//! retry policy, `Retry-After`, symbol fan-out, and the time-sliced planner
//! without any network access.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use lake_contracts::{
    format_nanos_utc, parse_iso_nanos, AssetClass, BarInterval, DateTime, Decimal18,
};
use lake_providers::{
    plan_time_slices, AlpacaClient, AlpacaTransport, BoxFuture, ProviderConfig, ProviderError,
    RequestGovernor, TransportFailure, TransportResponse,
};
use serde_json::{json, Value};

#[derive(Clone, Debug)]
struct SeenRequest {
    url: String,
    params: Vec<(String, String)>,
    headers: Vec<(String, String)>,
}

impl SeenRequest {
    fn param(&self, name: &str) -> Option<&str> {
        self.params
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }
}

type Outcome = Result<TransportResponse, TransportFailure>;
type Handler = Box<dyn Fn(&SeenRequest) -> Outcome + Send + Sync>;

struct FakeTransport {
    seen: Mutex<Vec<SeenRequest>>,
    handler: Handler,
}

impl FakeTransport {
    fn new(handler: impl Fn(&SeenRequest) -> Outcome + Send + Sync + 'static) -> Arc<Self> {
        Arc::new(Self {
            seen: Mutex::new(Vec::new()),
            handler: Box::new(handler),
        })
    }

    fn requests(&self) -> Vec<SeenRequest> {
        self.seen.lock().expect("recorded requests").clone()
    }
}

impl AlpacaTransport for FakeTransport {
    fn get<'a>(
        &'a self,
        url: &'a str,
        params: &'a [(String, String)],
        headers: &'a [(String, String)],
    ) -> BoxFuture<'a, Outcome> {
        let request = SeenRequest {
            url: url.to_string(),
            params: params.to_vec(),
            headers: headers.to_vec(),
        };
        self.seen.lock().expect("record").push(request.clone());
        let outcome = (self.handler)(&request);
        Box::pin(async move { outcome })
    }
}

fn ok(body: Value) -> Outcome {
    Ok(TransportResponse {
        status: 200,
        retry_after: None,
        body,
    })
}

fn status(code: u16) -> Outcome {
    Ok(TransportResponse {
        status: code,
        retry_after: None,
        body: Value::Null,
    })
}

fn retry_later(code: u16, retry_after: &str) -> Outcome {
    Ok(TransportResponse {
        status: code,
        retry_after: Some(retry_after.to_string()),
        body: Value::Null,
    })
}

fn instant(hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 11, 27, hour, minute, 0, 0).expect("instant")
}

fn at_14_30() -> DateTime {
    instant(14, 30)
}

fn at_14_31() -> DateTime {
    instant(14, 31)
}

fn at_15_00() -> DateTime {
    instant(15, 0)
}

fn trade_row(timestamp: &str, trade_id: u64) -> Value {
    json!({
        "t": timestamp,
        "i": trade_id,
        "x": "V",
        "p": "10.5",
        "s": 1,
        "c": [],
        "z": "C",
    })
}

fn quote_row(timestamp: &str) -> Value {
    json!({
        "t": timestamp,
        "bx": "V",
        "bp": "10.1",
        "bs": 1,
        "ax": "Q",
        "ap": "10.2",
        "as": 1,
        "c": [],
        "z": "C",
    })
}

fn crypto_trade_row(timestamp: &str) -> Value {
    json!({"t": timestamp, "p": "10.5", "s": "0.5"})
}

fn crypto_quote_row(timestamp: &str) -> Value {
    json!({"t": timestamp, "bp": "10.1", "bs": "0.1", "ap": "10.2", "as": "0.1"})
}

fn client(config: ProviderConfig, transport: Arc<FakeTransport>) -> AlpacaClient {
    let governor = RequestGovernor::new(config.requests_per_minute);
    AlpacaClient::new(config, transport, governor).expect("client")
}

fn fast_config() -> ProviderConfig {
    ProviderConfig {
        backoff_seconds: 0.0,
        ..ProviderConfig::default()
    }
}

fn pairs(params: &[(&str, &str)]) -> Vec<(String, String)> {
    params
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect()
}

#[tokio::test]
async fn equity_trades_request_shape_and_symbol_normalization() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": {"AAPL": []}})));
    let mut config = fast_config();
    config.api_key = Some("key".to_string());
    config.api_secret = Some("secret".to_string());
    let client = client(config, transport.clone());
    let start = DateTime::from_ymd_hms(2024, 11, 27, 14, 30, 0, 123_456).expect("start");
    let end = DateTime::from_ymd_hms(2024, 11, 27, 14, 31, 0, 123_456).expect("end");
    let records = client
        .fetch_trades(&[" aapl ", "AAPL", "msft"], start, end)
        .await
        .expect("fetch");
    assert!(records.is_empty());

    let requests = transport.requests();
    assert_eq!(requests.len(), 2, "duplicate symbols are deduplicated");
    let request = &requests[0];
    assert_eq!(request.url, "https://data.alpaca.markets/v2/stocks/trades");
    assert_eq!(
        request.params,
        pairs(&[
            ("start", "2024-11-27T14:30:00.123456Z"),
            ("end", "2024-11-27T14:31:00.123456Z"),
            ("limit", "10000"),
            ("sort", "asc"),
            ("symbols", "AAPL"),
            ("feed", "sip"),
        ])
    );
    assert_eq!(
        request.headers,
        pairs(&[
            ("Accept", "application/json"),
            ("Accept-Encoding", "gzip, br"),
            ("APCA-API-KEY-ID", "key"),
            ("APCA-API-SECRET-KEY", "secret"),
        ])
    );
    assert_eq!(requests[1].param("symbols"), Some("MSFT"));
}

#[tokio::test]
async fn equity_quotes_request_shape_supports_flat_pages() {
    let transport =
        FakeTransport::new(|_| ok(json!({"quotes": [quote_row("2024-11-27T14:30:00Z")]})));
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_quotes(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].symbol, "AAPL");

    let requests = transport.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0].url,
        "https://data.alpaca.markets/v2/stocks/quotes"
    );
    assert_eq!(requests[0].param("feed"), Some("sip"));
    assert_eq!(
        requests[0].headers,
        pairs(&[
            ("Accept", "application/json"),
            ("Accept-Encoding", "gzip, br"),
        ])
    );
}

#[tokio::test]
async fn crypto_request_shapes_omit_the_feed() {
    let transport = FakeTransport::new(|request| {
        if request.url.ends_with("/trades") {
            ok(json!({"trades": [crypto_trade_row("2024-11-27T14:30:00Z")]}))
        } else {
            ok(json!({"quotes": [crypto_quote_row("2024-11-27T14:30:00Z")]}))
        }
    });
    let client = client(fast_config(), transport.clone());
    let trades = client
        .fetch_crypto_trades(&["btc/usd", "BTC/USD"], at_14_30(), at_14_31())
        .await
        .expect("trades");
    let quotes = client
        .fetch_crypto_quotes(&["eth/usd"], at_14_30(), at_14_31())
        .await
        .expect("quotes");
    assert_eq!(trades.len(), 1);
    assert_eq!(quotes.len(), 1);

    let requests = transport.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(
        requests[0].url,
        "https://data.alpaca.markets/v1beta3/crypto/us/trades"
    );
    assert_eq!(requests[0].param("symbols"), Some("BTC/USD"));
    assert_eq!(requests[0].param("feed"), None);
    assert_eq!(requests[0].param("limit"), Some("10000"));
    assert_eq!(
        requests[1].url,
        "https://data.alpaca.markets/v1beta3/crypto/us/quotes"
    );
    assert_eq!(requests[1].param("symbols"), Some("ETH/USD"));
}

#[tokio::test]
async fn keyed_equity_pages_are_assembled_in_cursor_order() {
    let transport = FakeTransport::new(|request| match request.param("page_token") {
        None => ok(json!({
            "trades": {"AAPL": [trade_row("2024-11-27T14:30:00Z", 1)]},
            "next_page_token": "t1",
        })),
        Some("t1") => ok(json!({
            "trades": {"AAPL": [trade_row("2024-11-27T14:31:00Z", 2)]},
        })),
        Some(other) => panic!("unexpected page token {other}"),
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades_sliced(&["AAPL"], at_14_30(), at_15_00(), 1)
        .await
        .expect("fetch");
    assert_eq!(
        records
            .iter()
            .map(|record| record.trade_id)
            .collect::<Vec<_>>(),
        vec![1, 2]
    );
    let requests = transport.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].param("page_token"), None);
    assert_eq!(requests[1].param("page_token"), Some("t1"));
}

#[tokio::test]
async fn empty_next_page_token_stops_paging() {
    let transport = FakeTransport::new(|_| {
        ok(json!({"trades": [trade_row("2024-11-27T14:30:00Z", 1)], "next_page_token": ""}))
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(records.len(), 1);
    assert_eq!(transport.requests().len(), 1);
}

#[tokio::test]
async fn repeated_page_token_is_a_protocol_error() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": [], "next_page_token": "loop"})));
    let client = client(fast_config(), transport.clone());
    let error = client
        .fetch_trades_sliced(&["AAPL"], at_14_30(), at_15_00(), 1)
        .await
        .expect_err("looping cursor");
    match error {
        ProviderError::Protocol(message) => {
            assert!(
                message.contains("repeated Alpaca trades page token"),
                "{message}"
            );
            assert!(message.contains("AAPL"), "{message}");
        }
        other => panic!("expected protocol error, got {other:?}"),
    }
    assert_eq!(transport.requests().len(), 2);
}

#[tokio::test]
async fn retryable_status_is_retried_then_succeeds() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let transport = FakeTransport::new(move |_| {
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            status(500)
        } else {
            ok(json!({"trades": [trade_row("2024-11-27T14:30:00Z", 1)]}))
        }
    });
    let mut config = fast_config();
    config.max_attempts = 3;
    let client = client(config, transport.clone());
    let records = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(records.len(), 1);
    assert_eq!(transport.requests().len(), 2, "500 then 200");
}

#[tokio::test]
async fn exhausted_attempts_return_the_last_http_status() {
    let transport = FakeTransport::new(|_| status(503));
    let mut config = fast_config();
    config.max_attempts = 2;
    let client = client(config, transport.clone());
    let error = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect_err("exhausted");
    assert!(!error.is_transient());
    assert!(matches!(&error, ProviderError::Http { status: 503, .. }));
    assert_eq!(transport.requests().len(), 2);
}

#[tokio::test]
async fn non_retryable_status_fails_immediately() {
    let transport = FakeTransport::new(|_| status(400));
    let client = client(fast_config(), transport.clone());
    let error = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect_err("bad request");
    match error {
        ProviderError::Http { status, url } => {
            assert_eq!(status, 400);
            assert!(url.ends_with("/v2/stocks/trades"));
        }
        other => panic!("expected HTTP error, got {other:?}"),
    }
    assert_eq!(transport.requests().len(), 1, "400 is not retried");
}

#[tokio::test]
async fn transport_failures_are_retried_and_then_surfaced() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let transport = FakeTransport::new(move |_| {
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            Err(TransportFailure::Transport("connection reset".to_string()))
        } else {
            ok(json!({"trades": []}))
        }
    });
    let mut config = fast_config();
    config.max_attempts = 3;
    let retry_client = client(config, transport.clone());
    retry_client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("retried transport failure");
    assert_eq!(transport.requests().len(), 2);

    let transport =
        FakeTransport::new(|_| Err(TransportFailure::Timeout("read timeout".to_string())));
    let mut config = fast_config();
    config.max_attempts = 2;
    let timeout_client = client(config, transport.clone());
    let error = timeout_client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect_err("timeout");
    assert!(error.is_transient());
    assert!(matches!(&error, ProviderError::Timeout(message) if message == "read timeout"));
    assert_eq!(transport.requests().len(), 2);
}

#[tokio::test]
async fn protocol_failures_are_not_retried() {
    let transport =
        FakeTransport::new(|_| Err(TransportFailure::Protocol("malformed body".to_string())));
    let client = client(fast_config(), transport.clone());
    let error = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect_err("protocol failure");
    assert!(matches!(error, ProviderError::Protocol(message) if message == "malformed body"));
    assert_eq!(transport.requests().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn retry_after_is_honored_when_configured() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let transport = FakeTransport::new(move |_| {
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            retry_later(429, "2")
        } else {
            ok(json!({"trades": []}))
        }
    });
    let mut config = fast_config();
    config.max_attempts = 3;
    let client = client(config, transport.clone());
    let start = tokio::time::Instant::now();
    client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    let elapsed = start.elapsed();
    assert!(elapsed >= Duration::from_secs(2), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(3), "{elapsed:?}");
}

#[tokio::test(start_paused = true)]
async fn retry_after_is_ignored_when_disabled() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let transport = FakeTransport::new(move |_| {
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            retry_later(429, "2")
        } else {
            ok(json!({"trades": []}))
        }
    });
    let mut config = fast_config();
    config.honor_retry_after = false;
    let client = client(config, transport.clone());
    let start = tokio::time::Instant::now();
    client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(start.elapsed(), Duration::ZERO);
}

#[tokio::test(start_paused = true)]
async fn retry_after_is_clamped_to_the_max_backoff() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let transport = FakeTransport::new(move |_| {
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            retry_later(429, "100")
        } else {
            ok(json!({"trades": []}))
        }
    });
    let mut config = fast_config();
    config.max_backoff_seconds = 1.0;
    let client = client(config, transport.clone());
    let start = tokio::time::Instant::now();
    client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(start.elapsed(), Duration::from_secs(1));
}

#[tokio::test]
async fn one_symbol_failure_propagates_without_retrying() {
    let transport = FakeTransport::new(|request| {
        if request.param("symbols") == Some("FAIL") {
            Err(TransportFailure::Protocol("bad body".to_string()))
        } else {
            ok(json!({"trades": {"MSFT": [trade_row("2024-11-27T14:30:00Z", 1)]}}))
        }
    });
    let client = client(fast_config(), transport.clone());
    let error = client
        .fetch_trades(&["FAIL", "MSFT"], at_14_30(), at_14_31())
        .await
        .expect_err("one symbol failed");
    assert!(matches!(error, ProviderError::Protocol(message) if message == "bad body"));
    let requests = transport.requests();
    assert!(requests.len() <= 2, "{requests:#?}");
    assert!(
        requests
            .iter()
            .filter(|request| request.param("symbols") == Some("FAIL"))
            .count()
            == 1
    );
}

#[tokio::test]
async fn concurrent_symbols_are_all_fetched() {
    let transport = FakeTransport::new(|request| {
        let symbol = request.param("symbols").expect("symbol").to_string();
        let mut trades = serde_json::Map::new();
        trades.insert(symbol, json!([trade_row("2024-11-27T14:30:00Z", 1)]));
        ok(json!({"trades": Value::Object(trades)}))
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades(&["AAPL", "MSFT", "SPY"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(records.len(), 3);
    let symbols: Vec<_> = records
        .iter()
        .map(|record| record.symbol.as_str())
        .collect();
    assert_eq!(symbols, vec!["AAPL", "MSFT", "SPY"]);
}

#[tokio::test]
async fn time_sliced_fetches_cover_the_window_without_gaps_or_duplicates() {
    let transport = FakeTransport::new(|request| {
        let start = request.param("start").expect("start").to_string();
        let end = request.param("end").expect("end").to_string();
        match request.param("page_token") {
            None => ok(json!({
                "trades": [trade_row(&start, 1)],
                "next_page_token": "more",
            })),
            Some("more") => ok(json!({"trades": [trade_row(&end, 2)]})),
            Some(other) => panic!("unexpected page token {other}"),
        }
    });
    let client = client(fast_config(), transport.clone());
    let start = instant(14, 0);
    let records = client
        .fetch_trades_sliced(&["AAPL"], start, at_15_00(), 4)
        .await
        .expect("fetch");

    let mut expected: Vec<DateTime> = plan_time_slices(start, at_15_00(), 4)
        .iter()
        .map(|(slice_start, _)| *slice_start)
        .collect();
    expected.push(at_15_00());
    assert_eq!(
        records
            .iter()
            .map(|record| record.timestamp)
            .collect::<Vec<_>>(),
        expected
    );

    let requests = transport.requests();
    assert_eq!(requests.len(), 8, "each slice paginates independently");
    for (slice_start, slice_end) in plan_time_slices(start, at_15_00(), 4) {
        let slice_start = slice_start.isoformat_seconds_z();
        let slice_end = slice_end.isoformat_seconds_z();
        let slice_requests: Vec<_> = requests
            .iter()
            .filter(|request| request.param("start") == Some(slice_start.as_str()))
            .collect();
        assert_eq!(slice_requests.len(), 2);
        assert_eq!(slice_requests[0].param("page_token"), None);
        assert_eq!(slice_requests[1].param("page_token"), Some("more"));
        for request in slice_requests {
            assert_eq!(request.param("end"), Some(slice_end.as_str()));
        }
    }
}

#[tokio::test]
async fn single_slice_is_plain_sequential_paging() {
    let transport = FakeTransport::new(|request| match request.param("page_token") {
        None => ok(json!({
            "trades": [trade_row("2024-11-27T14:02:00Z", 2)],
            "next_page_token": "t1",
        })),
        Some("t1") => ok(json!({"trades": [trade_row("2024-11-27T14:01:00Z", 1)]})),
        Some(other) => panic!("unexpected page token {other}"),
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades_sliced(&["AAPL"], at_14_30(), at_15_00(), 1)
        .await
        .expect("fetch");
    assert_eq!(
        records
            .iter()
            .map(|record| record.trade_id)
            .collect::<Vec<_>>(),
        vec![2, 1],
        "a single slice keeps provider page order"
    );
    assert_eq!(transport.requests().len(), 2);
    assert_eq!(transport.requests()[0].param("page_token"), None);
    assert_eq!(transport.requests()[1].param("page_token"), Some("t1"));
}

#[tokio::test]
async fn a_cursor_splits_the_remainder_below_the_pages_final_timestamp() {
    // The first page holds two distinct timestamps and a cursor; the planner
    // must split at the largest timestamp strictly below the page's final row
    // (14:30:00), drop the trailing run it cannot prove complete, and let the
    // children re-fetch the remainder. No cursor is ever followed.
    let window_start = parse_iso_nanos("2024-11-27T14:30:00Z").expect("window start");
    let transport = FakeTransport::new(move |request| {
        assert!(
            request.param("page_token").is_none(),
            "the split planner must not follow the cursor"
        );
        let start = parse_iso_nanos(request.param("start").expect("start")).expect("start ns");
        let end = parse_iso_nanos(request.param("end").expect("end")).expect("end ns");
        if start == window_start && end > parse_iso_nanos("2024-11-27T14:31:00Z").expect("end") {
            unreachable!("the unit never exceeds its window");
        }
        if start == window_start && end == parse_iso_nanos("2024-11-27T14:31:00Z").expect("end") {
            // The whole-unit page reports a cursor after two rows.
            return ok(json!({
                "trades": [
                    trade_row("2024-11-27T14:30:00Z", 1),
                    trade_row("2024-11-27T14:30:30Z", 2),
                ],
                "next_page_token": "split-me",
            }));
        }
        let mut rows = Vec::new();
        for (timestamp, trade_id) in [
            ("2024-11-27T14:30:00Z", 1u64),
            ("2024-11-27T14:30:30Z", 2),
            ("2024-11-27T14:30:59Z", 3),
        ] {
            let nanos = parse_iso_nanos(timestamp).expect("row ns");
            if start <= nanos && nanos <= end {
                rows.push(trade_row(timestamp, trade_id));
            }
        }
        ok(json!({"trades": rows}))
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    let ids: Vec<u64> = records.iter().map(|record| record.trade_id).collect();
    assert_eq!(ids, vec![1, 2, 3], "complete, ordered, no duplicates");

    let requests = transport.requests();
    assert_eq!(requests.len(), 3, "one page plus exactly two children");
    assert_eq!(requests[0].param("start"), Some("2024-11-27T14:30:00Z"));
    assert_eq!(requests[0].param("end"), Some("2024-11-27T14:31:00Z"));

    let mut children: Vec<(i64, i64)> = requests[1..]
        .iter()
        .map(|request| {
            (
                parse_iso_nanos(request.param("start").expect("start")).expect("start ns"),
                parse_iso_nanos(request.param("end").expect("end")).expect("end ns"),
            )
        })
        .collect();
    children.sort();
    assert_eq!(
        children[0].0,
        parse_iso_nanos("2024-11-27T14:30:00.000000001Z").expect("plus one ns"),
        "the remainder starts one nanosecond past the safe boundary"
    );
    assert_eq!(
        children[1].0,
        children[0].1 + 1,
        "child units are one nanosecond apart"
    );
    assert_eq!(
        children[1].1,
        parse_iso_nanos("2024-11-27T14:31:00Z").expect("window end"),
        "the final child ends at the window end"
    );
}

#[tokio::test]
async fn a_cursor_split_never_loses_rows_tied_with_the_page_boundary() {
    // The page ends inside a three-row timestamp tie; the rows beyond the page
    // share the page's final timestamp. Splitting at that timestamp + 1ns
    // would orphan them, so they must be re-fetched by the children.
    let window_start = parse_iso_nanos("2024-11-27T14:30:00Z").expect("window start");
    let tied = parse_iso_nanos("2024-11-27T14:30:30Z").expect("tied");
    let transport = FakeTransport::new(move |request| {
        assert!(request.param("page_token").is_none());
        let start = parse_iso_nanos(request.param("start").expect("start")).expect("start ns");
        let end = parse_iso_nanos(request.param("end").expect("end")).expect("end ns");
        if start == window_start && end == parse_iso_nanos("2024-11-27T14:31:00Z").expect("end") {
            return ok(json!({
                "trades": [
                    trade_row("2024-11-27T14:30:00Z", 1),
                    trade_row("2024-11-27T14:30:30Z", 2),
                    trade_row("2024-11-27T14:30:30Z", 3),
                ],
                "next_page_token": "split-me",
            }));
        }
        let mut rows = Vec::new();
        for (nanos, trade_id) in [
            (parse_iso_nanos("2024-11-27T14:30:00Z").expect("row"), 1u64),
            (tied, 2),
            (tied, 3),
            (tied, 4),
        ] {
            if start <= nanos && nanos <= end {
                let timestamp = format_nanos_utc(nanos);
                rows.push(trade_row(&timestamp, trade_id));
            }
        }
        ok(json!({"trades": rows}))
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    let ids: Vec<u64> = records.iter().map(|record| record.trade_id).collect();
    assert_eq!(
        ids,
        vec![1, 2, 3, 4],
        "every tied row beyond the page must survive the split"
    );
}

#[tokio::test]
async fn a_single_page_window_is_one_request() {
    let transport =
        FakeTransport::new(|_| ok(json!({"trades": [trade_row("2024-11-27T14:30:00Z", 1)]})));
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_trades(&["AAPL"], at_14_30(), at_14_31())
        .await
        .expect("fetch");
    assert_eq!(records.len(), 1);
    assert_eq!(transport.requests().len(), 1);
}

#[tokio::test]
async fn zero_slices_is_a_configuration_error() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": []})));
    let client = client(fast_config(), transport.clone());
    let error = client
        .fetch_trades_sliced(&["AAPL"], at_14_30(), at_15_00(), 0)
        .await
        .expect_err("zero slices");
    assert!(matches!(error, ProviderError::Configuration(_)));
    assert!(transport.requests().is_empty());
}

#[tokio::test(start_paused = true)]
async fn every_attempt_consumes_the_governor_budget() {
    let transport =
        FakeTransport::new(|_| ok(json!({"trades": [trade_row("2024-11-27T14:30:00Z", 1)]})));
    let mut config = fast_config();
    config.requests_per_minute = Some(60);
    let client = client(config, transport.clone());
    let start = tokio::time::Instant::now();
    client
        .fetch_trades_sliced(&["AAPL"], at_14_30(), at_15_00(), 2)
        .await
        .expect("fetch");
    let elapsed = start.elapsed();
    // Two attempts at a 60-per-minute budget: the first is immediate and the
    // second waits out the token bucket interval.
    assert!(elapsed >= Duration::from_secs(1), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
}

#[tokio::test]
async fn invalid_symbols_and_pairs_fail_before_any_request() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": []})));
    let client = client(fast_config(), transport.clone());

    for symbols in [vec![""], vec!["AA PL"], vec!["   "]] {
        let error = client
            .fetch_trades(&symbols, at_14_30(), at_14_31())
            .await
            .expect_err("invalid symbol");
        assert!(matches!(error, ProviderError::Configuration(_)));
    }
    for pairs in [vec!["BTCUSD"], vec!["BTC/"], vec!["/USD"], vec!["BTC/US D"]] {
        let error = client
            .fetch_crypto_trades(&pairs, at_14_30(), at_14_31())
            .await
            .expect_err("invalid pair");
        assert!(matches!(error, ProviderError::Configuration(_)));
    }
    assert!(transport.requests().is_empty());
}

#[tokio::test]
async fn config_returns_the_normalized_client_configuration() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": []})));
    let config = ProviderConfig::new("https://data.alpaca.markets/", None, None);
    let client = client(config, transport);
    assert_eq!(client.config().base_url, "https://data.alpaca.markets");
}

#[tokio::test]
async fn with_reqwest_builds_a_configured_client() {
    let config = ProviderConfig {
        max_concurrency: 1,
        ..fast_config()
    };
    let client = AlpacaClient::with_reqwest(config, RequestGovernor::new(None)).expect("client");
    assert_eq!(client.config().max_concurrency, 1);
}

fn jan19(hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 19, hour, minute, 0, 0).expect("instant")
}

fn option_contract(
    symbol: &str,
    option_type: lake_catalog::OptionType,
) -> lake_catalog::ContractRecord {
    lake_catalog::ContractRecord::new(
        symbol,
        "SPY",
        jan19(0, 0).date(),
        lake_contracts::Decimal18::parse("500").expect("strike"),
        option_type,
        100,
        None,
        None,
    )
    .expect("contract")
}

fn option_trade_row(timestamp: &str) -> Value {
    json!({"t": timestamp, "x": "XOPR", "p": "2.5", "s": 3, "c": "I"})
}

#[tokio::test]
async fn option_trade_request_shape_omits_the_feed_and_uses_option_pages() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": {}})));
    let client = client(fast_config(), transport.clone());
    let contracts = vec![option_contract(
        "SPY240119C00500000",
        lake_catalog::OptionType::Call,
    )];
    client
        .fetch_option_trades(&contracts, jan19(14, 30), jan19(21, 0))
        .await
        .expect("fetch");

    let requests = transport.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0].url,
        "https://data.alpaca.markets/v1beta1/options/trades"
    );
    assert_eq!(
        requests[0].params,
        pairs(&[
            ("symbols", "SPY240119C00500000"),
            ("start", "2024-01-19T14:30:00Z"),
            ("end", "2024-01-19T21:00:00Z"),
            ("limit", "1000"),
            ("sort", "asc"),
        ])
    );
    assert_eq!(requests[0].param("feed"), None);
}

#[tokio::test]
async fn option_quotes_send_the_opra_feed_and_normalize_single_conditions() {
    let transport = FakeTransport::new(|_| {
        ok(json!({"quotes": {"SPY240119C00500000": [json!({
            "t": "2024-01-19T14:31:00Z",
            "bx": "XOPR",
            "bp": "2.4",
            "bs": 4,
            "ax": "CBOE",
            "ap": "2.6",
            "as": 5,
            "c": "A",
        })]}}))
    });
    let client = client(fast_config(), transport.clone());
    let contract = option_contract("SPY240119C00500000", lake_catalog::OptionType::Call);
    let quotes = client
        .fetch_option_quotes(&[contract], jan19(14, 30), jan19(21, 0))
        .await
        .expect("fetch");

    assert_eq!(quotes.len(), 1);
    assert_eq!(quotes[0].symbol, "SPY240119C00500000");
    assert_eq!(quotes[0].underlying, "SPY");
    assert_eq!(quotes[0].condition, "A");
    assert_eq!(quotes[0].bid_size, 4);
    assert_eq!(quotes[0].ask_exchange, "CBOE");

    let requests = transport.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0].url,
        "https://data.alpaca.markets/v1beta1/options/quotes"
    );
    assert_eq!(requests[0].param("feed"), Some("opra"));
    assert_eq!(requests[0].param("limit"), Some("1000"));
}

#[tokio::test]
async fn option_trades_page_the_cursor_sequentially_and_deduplicate_contracts() {
    let transport = FakeTransport::new(|request| match request.param("page_token") {
        None => ok(json!({
            "trades": {"SPY240119C00500000": [option_trade_row("2024-01-19T14:31:00Z")]},
            "next_page_token": "t1",
        })),
        Some("t1") => ok(json!({
            "trades": {"SPY240119C00500000": [option_trade_row("2024-01-19T14:32:00Z")]},
            "next_page_token": "",
        })),
        Some(other) => panic!("unexpected page token {other}"),
    });
    let client = client(fast_config(), transport.clone());
    let contract = option_contract("SPY240119C00500000", lake_catalog::OptionType::Call);
    let trades = client
        .fetch_option_trades(&[contract.clone(), contract], jan19(14, 30), jan19(21, 0))
        .await
        .expect("fetch");

    let timestamps: Vec<_> = trades.iter().map(|trade| trade.timestamp).collect();
    assert_eq!(
        timestamps,
        vec![jan19(14, 31), jan19(14, 32)],
        "provider page order, no duplicate-symbol second batch"
    );
    let requests = transport.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].param("symbols"), Some("SPY240119C00500000"));
    assert_eq!(requests[0].param("page_token"), None);
    assert_eq!(requests[1].param("page_token"), Some("t1"));
    assert_eq!(requests[1].param("feed"), None);
}

#[tokio::test]
async fn option_contracts_are_batched_one_hundred_per_request() {
    let contracts: Vec<_> = (0..101)
        .map(|index| {
            option_contract(
                &format!("SPY240119C00{index:05}"),
                lake_catalog::OptionType::Call,
            )
        })
        .collect();
    let transport = FakeTransport::new(|_| ok(json!({"trades": {}})));
    let client = client(fast_config(), transport.clone());
    client
        .fetch_option_trades(&contracts, jan19(14, 30), jan19(21, 0))
        .await
        .expect("fetch");

    let requests = transport.requests();
    assert_eq!(requests.len(), 2, "one batch per 100 contracts");
    assert_eq!(
        requests[0]
            .param("symbols")
            .expect("symbols")
            .split(',')
            .count(),
        100
    );
    assert_eq!(
        requests[1]
            .param("symbols")
            .expect("symbols")
            .split(',')
            .count(),
        1
    );
}

#[tokio::test]
async fn option_unselected_symbols_are_protocol_errors() {
    let transport = FakeTransport::new(|_| ok(json!({"trades": {"QQQ240119C00500000": []}})));
    let client = client(fast_config(), transport.clone());
    let contract = option_contract("SPY240119C00500000", lake_catalog::OptionType::Call);
    let error = client
        .fetch_option_trades(&[contract], jan19(14, 30), jan19(21, 0))
        .await
        .expect_err("unselected symbol");
    match error {
        ProviderError::Protocol(message) => {
            assert!(
                message.contains("unselected symbol 'QQQ240119C00500000'"),
                "{message}"
            );
        }
        other => panic!("expected protocol error, got {other:?}"),
    }
    assert_eq!(transport.requests().len(), 1);
}

#[tokio::test(start_paused = true)]
async fn option_fetches_consume_the_governor_and_honor_retry_after() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let transport = FakeTransport::new(move |_| {
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            retry_later(429, "2")
        } else {
            ok(
                json!({"trades": {"SPY240119C00500000": [option_trade_row("2024-01-19T14:31:00Z")]}}),
            )
        }
    });
    let mut config = fast_config();
    config.max_attempts = 3;
    let client = client(config, transport.clone());
    let contract = option_contract("SPY240119C00500000", lake_catalog::OptionType::Call);
    let trades = client
        .fetch_option_trades(&[contract], jan19(14, 30), jan19(21, 0))
        .await
        .expect("fetch");
    assert_eq!(trades.len(), 1);
    assert_eq!(transport.requests().len(), 2, "429 then 200");
}

#[tokio::test]
async fn option_repeated_page_token_is_a_protocol_error() {
    let transport = FakeTransport::new(|_| {
        ok(json!({"trades": {"SPY240119C00500000": []}, "next_page_token": "loop"}))
    });
    let client = client(fast_config(), transport.clone());
    let contract = option_contract("SPY240119C00500000", lake_catalog::OptionType::Call);
    let error = client
        .fetch_option_trades(&[contract], jan19(14, 30), jan19(21, 0))
        .await
        .expect_err("looping cursor");
    match error {
        ProviderError::Protocol(message) => {
            assert!(
                message.contains("repeated Alpaca option trades page token"),
                "{message}"
            );
        }
        other => panic!("expected protocol error, got {other:?}"),
    }
    assert_eq!(transport.requests().len(), 2);
}

fn equity_bars_page1() -> Value {
    serde_json::from_str(include_str!("fixtures/alpaca_equity_bars_page1.json"))
        .expect("equity bars page 1 fixture")
}

fn equity_bars_page2() -> Value {
    serde_json::from_str(include_str!("fixtures/alpaca_equity_bars_page2.json"))
        .expect("equity bars page 2 fixture")
}

fn crypto_bars_page() -> Value {
    serde_json::from_str(include_str!("fixtures/alpaca_crypto_bars_page.json"))
        .expect("crypto bars fixture")
}

#[tokio::test]
async fn equity_bars_use_the_symbol_path_with_raw_adjustment() {
    let page = equity_bars_page1();
    let transport = FakeTransport::new(move |_| {
        let mut body = page.clone();
        body["next_page_token"] = Value::Null;
        ok(body)
    });
    let client = client(fast_config(), transport.clone());
    let start = instant(14, 30);
    let records = client
        .fetch_bars(
            AssetClass::Equity,
            " aapl ",
            BarInterval::OneMinute,
            start,
            at_15_00(),
        )
        .await
        .expect("fetch");
    assert_eq!(records.len(), 2);
    assert_eq!(records[0].symbol, "AAPL");
    assert_eq!(records[0].timestamp, start);
    assert_eq!(records[0].open, Decimal18::parse("190.12").expect("open"));
    assert_eq!(records[1].volume, Decimal18::parse("900").expect("volume"));

    let requests = transport.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0].url,
        "https://data.alpaca.markets/v2/stocks/AAPL/bars"
    );
    assert_eq!(
        requests[0].params,
        pairs(&[
            ("start", "2024-11-27T14:30:00Z"),
            ("end", "2024-11-27T15:00:00Z"),
            ("limit", "10000"),
            ("sort", "asc"),
            ("timeframe", "1Min"),
            ("adjustment", "raw"),
            ("feed", "sip"),
        ])
    );
}

#[tokio::test]
async fn equity_bars_follow_the_next_page_token() {
    let first = equity_bars_page1();
    let second = equity_bars_page2();
    let transport = FakeTransport::new(move |request| match request.param("page_token") {
        None => ok(first.clone()),
        Some("bars-page-2") => ok(second.clone()),
        Some(other) => panic!("unexpected page token {other}"),
    });
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_bars(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            at_14_30(),
            at_15_00(),
        )
        .await
        .expect("fetch");
    assert_eq!(
        records
            .iter()
            .map(|record| record.timestamp)
            .collect::<Vec<_>>(),
        vec![instant(14, 30), instant(14, 31), instant(14, 32)]
    );
    let requests = transport.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].param("page_token"), None);
    assert_eq!(requests[1].param("page_token"), Some("bars-page-2"));
}

#[tokio::test]
async fn equity_daily_bars_request_the_day_timeframe() {
    let transport = FakeTransport::new(|_| {
        ok(
            serde_json::from_str(include_str!("fixtures/alpaca_equity_daily_bars.json"))
                .expect("daily fixture"),
        )
    });
    let client = client(fast_config(), transport.clone());
    client
        .fetch_bars(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneDay,
            at_14_30(),
            at_15_00(),
        )
        .await
        .expect("fetch");
    assert_eq!(transport.requests()[0].param("timeframe"), Some("1Day"));
    assert_eq!(transport.requests()[0].param("adjustment"), Some("raw"));
}

#[tokio::test]
async fn crypto_bars_use_the_crypto_endpoint_without_feed_or_adjustment() {
    let transport = FakeTransport::new(|_| ok(crypto_bars_page()));
    let client = client(fast_config(), transport.clone());
    let records = client
        .fetch_bars(
            AssetClass::Crypto,
            "btc/usd",
            BarInterval::OneMinute,
            at_14_30(),
            at_14_31(),
        )
        .await
        .expect("fetch");
    assert_eq!(records.len(), 2);
    assert_eq!(records[0].symbol, "BTC/USD");
    assert_eq!(records[1].volume, Decimal18::parse("0.25").expect("volume"));

    let requests = transport.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0].url,
        "https://data.alpaca.markets/v1beta3/crypto/us/bars"
    );
    assert_eq!(
        requests[0].params,
        pairs(&[
            ("symbols", "BTC/USD"),
            ("start", "2024-11-27T14:30:00Z"),
            ("end", "2024-11-27T14:31:00Z"),
            ("limit", "10000"),
            ("sort", "asc"),
            ("timeframe", "1Min"),
        ])
    );
}

#[tokio::test]
async fn repeated_bars_page_token_is_a_protocol_error() {
    let transport =
        FakeTransport::new(|_| ok(json!({"bars": {"AAPL": []}, "next_page_token": "loop"})));
    let client = client(fast_config(), transport.clone());
    let error = client
        .fetch_bars(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneDay,
            at_14_30(),
            at_14_31(),
        )
        .await
        .expect_err("looping cursor");
    match error {
        ProviderError::Protocol(message) => {
            assert!(
                message.contains("repeated Alpaca bars page token"),
                "{message}"
            );
            assert!(message.contains("AAPL"), "{message}");
        }
        other => panic!("expected protocol error, got {other:?}"),
    }
    assert_eq!(transport.requests().len(), 2);
}

#[tokio::test]
async fn bars_reject_option_and_reference_asset_classes_before_any_request() {
    let transport = FakeTransport::new(|_| ok(json!({"bars": []})));
    let client = client(fast_config(), transport.clone());
    for asset_class in [AssetClass::Option, AssetClass::Reference] {
        let error = client
            .fetch_bars(
                asset_class,
                "SPY",
                BarInterval::OneDay,
                at_14_30(),
                at_14_31(),
            )
            .await
            .expect_err("unsupported asset class");
        assert!(matches!(error, ProviderError::Configuration(_)));
    }
    assert!(transport.requests().is_empty());
}
