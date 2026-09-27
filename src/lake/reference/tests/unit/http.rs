use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use lake_contracts::DateTime;
use serde_json::json;

use super::*;

const SEC_USER_AGENT: &str = "FCA market lake ingest AdminContact@example.com";
const TARGET_URL: &str = "https://data.sec.gov/api/xbrl/companyfacts/CIK0003200193.json";

/// A float clock the test drives by hand.
struct FakeFloatClock(Mutex<f64>);

impl FakeFloatClock {
    fn new() -> Arc<Self> {
        Arc::new(Self(Mutex::new(0.0)))
    }
}

impl FakeFloatClock {
    fn advance(&self, seconds: f64) {
        *self.0.lock().expect("clock") += seconds;
    }

    fn moment(&self) -> f64 {
        *self.0.lock().expect("clock")
    }
}

impl FloatClock for FakeFloatClock {
    fn now(&self) -> f64 {
        self.moment()
    }
}

/// A sleep recorder that advances the paired float clock.
struct FakeSleep {
    clock: Arc<FakeFloatClock>,
    waits: Mutex<Vec<f64>>,
}

impl FakeSleep {
    fn new(clock: Arc<FakeFloatClock>) -> Arc<Self> {
        Arc::new(Self {
            waits: Mutex::new(Vec::new()),
            clock,
        })
    }

    fn waits(&self) -> Vec<f64> {
        self.waits.lock().expect("waits").clone()
    }
}

impl Sleeper for FakeSleep {
    fn sleep(&self, seconds: f64) -> BoxSleepFuture<'_> {
        self.waits.lock().expect("waits").push(seconds);
        self.clock.advance(seconds);
        Box::pin(async {})
    }
}

struct FakeResponse {
    status: u16,
    headers: Vec<(String, String)>,
    content: Vec<u8>,
}

struct RecordedRequest {
    url: String,
    headers: Vec<(String, String)>,
    timeout: f64,
}

impl Clone for RecordedRequest {
    fn clone(&self) -> Self {
        Self {
            url: self.url.clone(),
            headers: self.headers.clone(),
            timeout: self.timeout,
        }
    }
}

struct FakeTransport {
    outcomes: Mutex<VecDeque<Result<FakeResponse, String>>>,
    requests: Mutex<Vec<RecordedRequest>>,
}

impl FakeTransport {
    fn new(outcomes: Vec<Result<FakeResponse, String>>) -> Arc<Self> {
        Arc::new(Self {
            outcomes: Mutex::new(outcomes.into()),
            requests: Mutex::new(Vec::new()),
        })
    }

    fn requests(&self) -> Vec<RecordedRequest> {
        self.requests.lock().expect("requests").clone()
    }
}

fn ok_response(status: u16, content: &[u8]) -> Result<FakeResponse, String> {
    Ok(FakeResponse {
        status,
        headers: Vec::new(),
        content: content.to_vec(),
    })
}

impl ReferenceTransport for FakeTransport {
    fn request<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        timeout_seconds: f64,
    ) -> BoxRequestFuture<'a> {
        self.requests
            .lock()
            .expect("requests")
            .push(RecordedRequest {
                url: url.to_string(),
                headers: headers.to_vec(),
                timeout: timeout_seconds,
            });
        let outcome = self
            .outcomes
            .lock()
            .expect("outcomes")
            .pop_front()
            .expect("outcome");
        Box::pin(async move {
            match outcome {
                Ok(response) => Ok(TransportResponse {
                    status: response.status,
                    headers: response.headers,
                    content: response.content,
                }),
                Err(message) => Err(TransportFailure::Transport(message)),
            }
        })
    }
}

/// Wrap the shared fake transport behind an owned trait object.
struct SharedTransport(Arc<FakeTransport>);

impl ReferenceTransport for SharedTransport {
    fn request<'a>(
        &'a self,
        url: &'a str,
        headers: &'a [(String, String)],
        timeout_seconds: f64,
    ) -> BoxRequestFuture<'a> {
        self.0.request(url, headers, timeout_seconds)
    }
}

/// Share one sleep recorder with both the limiter and the client.
struct SharedSleeper(Arc<FakeSleep>);

impl Sleeper for SharedSleeper {
    fn sleep(&self, seconds: f64) -> BoxSleepFuture<'_> {
        self.0.sleep(seconds)
    }
}

/// Share one float clock with both the limiter and the client.
struct SharedClock(Arc<FakeFloatClock>);

impl FloatClock for SharedClock {
    fn now(&self) -> f64 {
        self.0.now()
    }
}

fn limiter(clock: &Arc<FakeFloatClock>, sleeper: &Arc<FakeSleep>) -> RateLimiter {
    RateLimiter::with_pacing(
        10.0,
        Box::new(SharedClock(clock.clone())),
        Box::new(SharedSleeper(sleeper.clone())),
    )
    .expect("limiter")
}

struct FixedAt(DateTime);

impl DatetimeClock for FixedAt {
    fn now(&self) -> DateTime {
        self.0
    }
}

#[test]
fn default_rate_is_the_sec_fair_access_cap() {
    assert!((DEFAULT_REQUESTS_PER_SECOND - 10.0).abs() < f64::EPSILON);
}

#[tokio::test]
async fn rate_limiter_paces_requests_at_one_per_min_interval() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let limiter = RateLimiter::with_pacing(
        10.0,
        Box::new(SharedClock(clock.clone())),
        Box::new(SharedSleeper(sleep.clone())),
    )
    .expect("limiter");

    for _ in 0..4 {
        limiter.acquire().await;
    }

    let waits = sleep.waits();
    assert_eq!(waits.len(), 3);
    assert!(waits.iter().all(|wait| (wait - 0.1).abs() < 1e-9));
}

#[tokio::test]
async fn rate_limiter_serializes_concurrent_callers() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let limiter = Arc::new(
        RateLimiter::with_pacing(
            10.0,
            Box::new(SharedClock(clock.clone())),
            Box::new(SharedSleeper(sleep.clone())),
        )
        .expect("limiter"),
    );

    let mut handles = Vec::new();
    for _ in 0..10 {
        let limiter = limiter.clone();
        handles.push(tokio::spawn(async move { limiter.acquire().await }));
    }
    for handle in handles {
        handle.await.expect("task");
    }

    let waits = sleep.waits();
    assert_eq!(waits.len(), 9);
    assert!(waits.iter().all(|wait| (wait - 0.1).abs() < 1e-9));
    assert!((clock.moment() - 0.9).abs() < 1e-9);
}

#[test]
fn rate_limiter_rejects_non_positive_rates() {
    for rate in [0.0, -1.0] {
        let error = RateLimiter::new(rate).expect_err("rejected");
        assert!(error
            .to_string()
            .contains("requests_per_second must be positive"));
    }
}

#[tokio::test]
async fn client_sends_descriptive_user_agent_and_timeout() {
    let transport = FakeTransport::new(vec![Ok(FakeResponse {
        status: 200,
        headers: Vec::new(),
        content: b"payload".to_vec(),
    })]);
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .timeout_seconds(12.5)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport.clone())))
        .sleeper(Box::new(SharedSleeper(sleep)))
        .build()
        .expect("client");

    let payload = client.get_bytes(TARGET_URL).await.expect("payload");

    assert_eq!(payload, b"payload");
    let requests = transport.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].url, TARGET_URL);
    assert_eq!(
        requests[0].headers,
        vec![
            ("User-Agent".to_string(), SEC_USER_AGENT.to_string()),
            ("Accept-Encoding".to_string(), "gzip, deflate".to_string()),
        ]
    );
    assert!((requests[0].timeout - 12.5).abs() < f64::EPSILON);
}

#[tokio::test]
async fn client_retries_retryable_status_codes_with_backoff() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(vec![
        Ok(FakeResponse {
            status: 500,
            headers: Vec::new(),
            content: Vec::new(),
        }),
        Ok(FakeResponse {
            status: 503,
            headers: Vec::new(),
            content: Vec::new(),
        }),
        Ok(FakeResponse {
            status: 200,
            headers: Vec::new(),
            content: b"ok".to_vec(),
        }),
    ]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport)))
        .sleeper(Box::new(SharedSleeper(sleep.clone())))
        .build()
        .expect("client");

    let payload = client.get_bytes(TARGET_URL).await.expect("payload");

    assert_eq!(payload, b"ok");
    assert_eq!(sleep.waits(), vec![0.5, 1.0]);
}

#[tokio::test]
async fn client_raises_after_the_retry_budget_is_exhausted() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(
        (0..5)
            .map(|_| {
                Ok(FakeResponse {
                    status: 500,
                    headers: Vec::new(),
                    content: Vec::new(),
                })
            })
            .collect(),
    );
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport.clone())))
        .sleeper(Box::new(SharedSleeper(sleep.clone())))
        .build()
        .expect("client");

    let error = client.get_bytes(TARGET_URL).await.expect_err("exhausted");
    assert!(error.to_string().contains("HTTP 500"));
    assert_eq!(transport.requests().len(), 5);
    assert_eq!(sleep.waits(), vec![0.5, 1.0, 2.0, 4.0]);
}

#[tokio::test]
async fn client_fails_fast_on_non_retryable_status_codes() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(vec![Ok(FakeResponse {
        status: 404,
        headers: Vec::new(),
        content: Vec::new(),
    })]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport.clone())))
        .sleeper(Box::new(SharedSleeper(sleep)))
        .build()
        .expect("client");

    let error = client.get_bytes(TARGET_URL).await.expect_err("rejected");
    assert!(error.to_string().contains("HTTP 404"));
    assert_eq!(transport.requests().len(), 1);
}

#[tokio::test]
async fn client_honors_numeric_retry_after_on_rate_limit_responses() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(vec![
        Ok(FakeResponse {
            status: 429,
            headers: vec![("Retry-After".to_string(), "7".to_string())],
            content: Vec::new(),
        }),
        ok_response(200, b"ok"),
    ]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport)))
        .sleeper(Box::new(SharedSleeper(sleep.clone())))
        .clock(Box::new(EpochClock))
        .build()
        .expect("client");

    let payload = client.get_bytes(TARGET_URL).await.expect("payload");
    assert_eq!(payload, b"ok");
    assert_eq!(sleep.waits(), vec![7.0]);
}

#[tokio::test]
async fn client_honors_http_date_retry_after_values() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(vec![
        Ok(FakeResponse {
            status: 429,
            headers: vec![(
                "Retry-After".to_string(),
                "Mon, 01 Jan 2024 00:00:10 GMT".to_string(),
            )],
            content: Vec::new(),
        }),
        ok_response(200, b"ok"),
    ]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport)))
        .sleeper(Box::new(SharedSleeper(sleep.clone())))
        .clock(Box::new(FixedAt(
            DateTime::from_ymd_hms(2024, 1, 1, 0, 0, 7, 0).expect("now"),
        )))
        .build()
        .expect("client");

    let payload = client.get_bytes(TARGET_URL).await.expect("payload");
    assert_eq!(payload, b"ok");
    assert_eq!(sleep.waits(), vec![3.0]);
}

#[tokio::test]
async fn client_falls_back_to_backoff_on_unparsable_retry_after() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(vec![
        Ok(FakeResponse {
            status: 429,
            headers: vec![("Retry-After".to_string(), "garbage".to_string())],
            content: Vec::new(),
        }),
        ok_response(200, b"ok"),
    ]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport)))
        .sleeper(Box::new(SharedSleeper(sleep.clone())))
        .clock(Box::new(EpochClock))
        .build()
        .expect("client");

    let payload = client.get_bytes(TARGET_URL).await.expect("payload");
    assert_eq!(payload, b"ok");
    assert_eq!(sleep.waits(), vec![0.5]);
}

#[tokio::test]
async fn client_retries_transport_errors_then_raises() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let transport = FakeTransport::new(vec![
        Err("boom".to_string()),
        Err("slow".to_string()),
        Err("boom".to_string()),
    ]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .max_attempts(3)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(transport.clone())))
        .sleeper(Box::new(SharedSleeper(sleep.clone())))
        .build()
        .expect("client");

    let error = client.get_bytes(TARGET_URL).await.expect_err("exhausted");
    assert!(error.to_string().contains("boom"));
    assert_eq!(transport.requests().len(), 3);
    assert_eq!(sleep.waits(), vec![0.5, 1.0]);
}

#[tokio::test]
async fn client_decodes_json_and_rejects_malformed_payloads() {
    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let good = FakeTransport::new(vec![ok_response(200, br#"{"cik": 320193}"#)]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(good)))
        .sleeper(Box::new(SharedSleeper(sleep)))
        .build()
        .expect("client");
    let value = client.get_json(TARGET_URL).await.expect("json");
    assert_eq!(value, json!({"cik": 320193}));

    let clock = FakeFloatClock::new();
    let sleep = FakeSleep::new(clock.clone());
    let bad = FakeTransport::new(vec![ok_response(200, b"not json")]);
    let client = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .rate_limiter(limiter(&clock, &sleep))
        .transport(Box::new(SharedTransport(bad)))
        .sleeper(Box::new(SharedSleeper(sleep)))
        .build()
        .expect("client");
    let error = client.get_json(TARGET_URL).await.expect_err("rejected");
    assert!(error.to_string().contains(TARGET_URL));
}

#[test]
fn client_validates_the_sec_user_agent_format() {
    let error = ReferenceHTTPClient::builder("anonymous")
        .build()
        .expect_err("rejected");
    assert!(error.to_string().contains("user_agent"));
    let error = ReferenceHTTPClient::builder("   ")
        .build()
        .expect_err("rejected");
    assert!(error.to_string().contains("user_agent"));
}

#[test]
fn client_defaults_match_the_python_client() {
    let built = ReferenceHTTPClient::builder(SEC_USER_AGENT)
        .build()
        .expect("client");
    assert!((built.requests_per_second() - 10.0).abs() < f64::EPSILON);
    assert!((built.timeout_seconds() - 30.0).abs() < f64::EPSILON);
    assert_eq!(built.user_agent(), SEC_USER_AGENT);
}

#[test]
fn retry_after_seconds_parses_numbers_dates_and_garbage() {
    let headers = |value: &str| vec![("Retry-After".to_string(), value.to_string())];
    let now = DateTime::from_ymd_hms(2024, 1, 1, 0, 0, 7, 0).expect("now");

    assert_eq!(retry_after_seconds(&headers("7"), now), Some(7.0));
    assert_eq!(
        retry_after_seconds(&headers("Mon, 01 Jan 2024 00:00:10 GMT"), now),
        Some(3.0)
    );
    assert_eq!(retry_after_seconds(&headers("garbage"), now), None);
    assert_eq!(retry_after_seconds(&headers("-5"), now), Some(0.0));
    assert_eq!(retry_after_seconds(&[], now), None);
}

/// A fixed 2024-01-01T00:00:00Z wall clock.
struct EpochClock;

impl DatetimeClock for EpochClock {
    fn now(&self) -> DateTime {
        DateTime::from_ymd_hms(2024, 1, 1, 0, 0, 0, 0).expect("fixed")
    }
}
