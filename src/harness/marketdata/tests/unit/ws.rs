use super::*;

fn instant(hour: i64) -> DateTime {
    DateTime::from_micros(hour * 3_600 * 1_000_000)
}

#[test]
fn sip_inbox_normalizes_quote_and_sequence() {
    let mut inbox = AlpacaInbox::sip(0, 5_000_000_000, 300_000_000_000).unwrap();
    let messages = vec![AlpacaMessage::Quote {
        symbol: "aapl".to_string(),
        timestamp: instant(0).isoformat_micros() + "Z",
        bid_price: "10.5".to_string(),
        bid_size: "100".to_string(),
        ask_price: "10.6".to_string(),
        ask_size: "200".to_string(),
        venue: Some("V".to_string()),
    }];
    let events = inbox.push(&messages, instant(0)).unwrap();
    assert_eq!(events.len(), 1);
    match &events[0] {
        MarketEvent::Quote(quote) => {
            assert_eq!(quote.symbol, "AAPL");
            assert_eq!(quote.sequence, 0);
            assert!(quote.event_id.starts_with("alpaca-sip:q:AAPL:"));
        }
        _ => panic!("expected quote"),
    }
    assert_eq!(inbox.next_sequence(), 1);
}

#[test]
fn opra_subscription_rotates_to_snapshot() {
    let current =
        AlpacaOpraSubscription::new(vec!["AAPL240119C00150000".to_string()], Vec::new()).unwrap();
    let target =
        AlpacaOpraSubscription::new(vec!["AAPL240119C00160000".to_string()], Vec::new()).unwrap();
    let delta = rotation_delta(&current, &target);
    assert!(!delta.is_empty());
    assert_eq!(delta.unsubscribe_trades, vec!["AAPL240119C00150000"]);
    assert_eq!(delta.subscribe_trades, vec!["AAPL240119C00160000"]);
}

#[test]
fn backoff_is_bounded() {
    assert_eq!(backoff_delay(0, 0.5, 30.0), 0.0);
    assert_eq!(backoff_delay(1, 0.5, 30.0), 0.5);
    assert_eq!(backoff_delay(3, 0.5, 30.0), 2.0);
    assert_eq!(backoff_delay(40, 0.5, 30.0), 30.0);
}

#[test]
fn opra_rotation_payloads_match_python() {
    let current =
        AlpacaOpraSubscription::new(vec!["AAPL240119C00150000".to_string()], Vec::new()).unwrap();
    let target =
        AlpacaOpraSubscription::new(vec!["AAPL240119C00160000".to_string()], Vec::new()).unwrap();
    let payloads = opra_rotation_payloads(&rotation_delta(&current, &target));
    assert_eq!(
        payloads[0],
        "{\"action\":\"unsubscribe\",\"trades\":[\"AAPL240119C00150000\"],\"quotes\":[]}"
    );
    assert_eq!(
        payloads[1],
        "{\"action\":\"subscribe\",\"trades\":[\"AAPL240119C00160000\"],\"quotes\":[]}"
    );
}

struct FakeConnection {
    frames: std::sync::Mutex<VecDeque<Vec<u8>>>,
    sent: std::sync::Mutex<Vec<String>>,
}

impl FakeConnection {
    fn new(frames: Vec<Vec<u8>>) -> Self {
        Self {
            frames: std::sync::Mutex::new(frames.into()),
            sent: std::sync::Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl AsyncWebSocketConnection for FakeConnection {
    async fn send(&self, message: &str) -> Result<(), DataError> {
        self.sent.lock().unwrap().push(message.to_string());
        Ok(())
    }

    async fn receive(&self, _timeout_seconds: f64) -> Result<Option<Vec<u8>>, DataError> {
        match self.frames.lock().unwrap().pop_front() {
            Some(frame) => Ok(Some(frame)),
            None => Err(DataError::Protocol("fake closed".to_string())),
        }
    }

    async fn close(&self) -> Result<(), DataError> {
        Ok(())
    }
}

struct FakeTransport {
    connection: Arc<FakeConnection>,
}

#[async_trait]
impl AsyncWebSocketTransport for FakeTransport {
    async fn connect(&self, _url: &str) -> Result<Arc<dyn AsyncWebSocketConnection>, DataError> {
        Ok(self.connection.clone())
    }
}

#[tokio::test]
async fn live_feed_authenticates_subscribes_and_streams() {
    let stamp = DateTime::from_micros(0).isoformat_micros_utc();
    let frames = vec![
        serde_json::to_vec(&serde_json::json!([{"T": "success", "msg": "connected"}])).unwrap(),
        serde_json::to_vec(&serde_json::json!([{"T": "success", "msg": "authenticated"}])).unwrap(),
        serde_json::to_vec(&serde_json::json!([
            {"T": "subscription", "trades": ["AAPL"], "quotes": ["AAPL"]},
            {"T": "t", "S": "AAPL", "t": stamp, "p": "10.5", "s": "100", "x": "V", "i": "7"}
        ]))
        .unwrap(),
    ];
    let connection = Arc::new(FakeConnection::new(frames));
    let transport = Arc::new(FakeTransport {
        connection: Arc::clone(&connection),
    });
    let inbox = AlpacaInbox::sip(0, 5_000_000_000, 300_000_000_000).unwrap();
    let clock: AsyncClock = Arc::new(|| DateTime::from_micros(0));
    let config = AlpacaLiveConfig {
        api_key: "key".to_string(),
        api_secret: "secret".to_string(),
        kind: InboxKind::Sip,
        subscription: AlpacaSipSubscription::new(
            vec!["AAPL".to_string()],
            vec!["AAPL".to_string()],
        )
        .unwrap(),
        transport,
        inbox,
        clock,
        websocket_url: None,
        backoff_seconds: 0.5,
        max_backoff_seconds: 30.0,
        watchdog_interval: 1.0,
        stale_after: 30_000_000_000,
        handshake_timeout: 10_000_000_000,
        on_stale: None,
    };
    let mut feed = AlpacaLiveFeed::new(config).unwrap();
    let stop = StopSignal::new();
    let error = feed.run_connection(&stop).await.unwrap_err();
    assert_eq!(error.to_string(), "fake closed");
    assert_eq!(feed.inbox().len(), 1);
    let sent = connection.sent.lock().unwrap().clone();
    assert_eq!(
        sent[0],
        "{\"action\":\"auth\",\"key\":\"key\",\"secret\":\"secret\"}"
    );
    assert_eq!(
        sent[1],
        "{\"action\":\"subscribe\",\"trades\":[\"AAPL\"],\"quotes\":[\"AAPL\"]}"
    );
    match feed.inbox_mut().pop().unwrap() {
        MarketEvent::Trade(trade) => {
            assert_eq!(trade.symbol, "AAPL");
            assert_eq!(trade.trade_id.as_deref(), Some("7"));
            assert_eq!(trade.venue.as_deref(), Some("V"));
        }
        _ => panic!("expected trade"),
    }
}
