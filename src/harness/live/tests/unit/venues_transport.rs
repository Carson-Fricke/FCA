use std::sync::Mutex;

use super::{
    Clock, RetryConfig, RetryingBrokerTransport, ScriptedFrameStream, SleepFn, SystemClock,
};
use crate::error::LiveResult;
use crate::venues::alpaca::{AsyncBrokerTransport, TransportResponse};

#[derive(Default)]
struct RecordingSleep {
    delays: Mutex<Vec<f64>>,
}

impl RecordingSleep {
    fn sleep_fn(self: &std::sync::Arc<Self>) -> SleepFn {
        let recorder = std::sync::Arc::clone(self);
        std::sync::Arc::new(move |seconds: f64| {
            recorder.delays.lock().unwrap().push(seconds);
            Box::pin(async move {})
        })
    }

    fn delays(&self) -> Vec<f64> {
        self.delays.lock().unwrap().clone()
    }
}

#[derive(Default)]
struct ScriptedTransport {
    responses: Mutex<Vec<LiveResult<TransportResponse>>>,
    calls: Mutex<usize>,
}

impl ScriptedTransport {
    fn new(responses: Vec<LiveResult<TransportResponse>>) -> Self {
        Self {
            responses: Mutex::new(responses),
            calls: Mutex::new(0),
        }
    }

    fn calls(&self) -> usize {
        *self.calls.lock().unwrap()
    }
}

impl AsyncBrokerTransport for ScriptedTransport {
    async fn request(
        &self,
        _method: &str,
        _url: &str,
        _body: Option<String>,
    ) -> LiveResult<TransportResponse> {
        *self.calls.lock().unwrap() += 1;
        let mut responses = self.responses.lock().unwrap();
        if responses.is_empty() {
            return Err(crate::error::LiveError::Venue("exhausted".into()));
        }
        responses.remove(0)
    }
}

fn response(status: u16, retry_after: Option<&str>) -> TransportResponse {
    TransportResponse {
        status,
        body: String::new(),
        headers: retry_after
            .map(|value| vec![("Retry-After".to_string(), value.to_string())])
            .unwrap_or_default(),
    }
}

#[test]
fn backoff_is_exponential_and_capped() {
    let config = RetryConfig::new(5, 0.25, 1.0, true, 30.0).unwrap();
    assert_eq!(config.backoff(1), 0.25);
    assert_eq!(config.backoff(2), 0.5);
    assert_eq!(config.backoff(3), 1.0);
    assert_eq!(config.backoff(6), 1.0);
}

#[test]
fn invalid_retry_config_is_rejected() {
    assert!(RetryConfig::new(0, 0.25, 1.0, true, 30.0).is_err());
    assert!(RetryConfig::new(5, -1.0, 1.0, true, 30.0).is_err());
    assert!(RetryConfig::new(5, 0.25, 1.0, true, 0.0).is_err());
}

#[tokio::test]
async fn retries_retryable_status_then_succeeds() {
    let inner = ScriptedTransport::new(vec![Ok(response(503, None)), Ok(response(200, None))]);
    let config = RetryConfig::new(5, 0.25, 30.0, true, 30.0).unwrap();
    let sleep = std::sync::Arc::new(RecordingSleep::default());
    let transport = RetryingBrokerTransport::with_sleep(inner, config, sleep.sleep_fn());
    let result = transport.request("GET", "http://example", None).await;
    assert_eq!(result.unwrap().status, 200);
    assert_eq!(transport.inner.calls(), 2);
    assert_eq!(sleep.delays(), vec![0.25]);
}

#[tokio::test]
async fn honors_retry_after_on_429() {
    let inner = ScriptedTransport::new(vec![
        Ok(response(429, Some("2.5"))),
        Ok(response(200, None)),
    ]);
    let config = RetryConfig::new(5, 0.25, 30.0, true, 30.0).unwrap();
    let sleep = std::sync::Arc::new(RecordingSleep::default());
    let transport = RetryingBrokerTransport::with_sleep(inner, config, sleep.sleep_fn());
    transport
        .request("GET", "http://example", None)
        .await
        .unwrap();
    assert_eq!(sleep.delays(), vec![2.5]);
}

#[tokio::test]
async fn non_retryable_status_surfaces_immediately() {
    let inner = ScriptedTransport::new(vec![Ok(response(400, None))]);
    let config = RetryConfig::new(5, 0.25, 30.0, true, 30.0).unwrap();
    let sleep = std::sync::Arc::new(RecordingSleep::default());
    let transport = RetryingBrokerTransport::with_sleep(inner, config, sleep.sleep_fn());
    assert!(transport
        .request("GET", "http://example", None)
        .await
        .is_err());
    assert_eq!(transport.inner.calls(), 1);
    assert!(sleep.delays().is_empty());
}

#[tokio::test]
async fn exhausts_attempts_on_persistent_transport_error() {
    let inner = ScriptedTransport::new(vec![
        Err(crate::error::LiveError::Venue("a".into())),
        Err(crate::error::LiveError::Venue("b".into())),
    ]);
    let config = RetryConfig::new(2, 0.5, 30.0, true, 30.0).unwrap();
    let sleep = std::sync::Arc::new(RecordingSleep::default());
    let transport = RetryingBrokerTransport::with_sleep(inner, config, sleep.sleep_fn());
    assert!(transport
        .request("GET", "http://example", None)
        .await
        .is_err());
    assert_eq!(transport.inner.calls(), 2);
    assert_eq!(sleep.delays(), vec![0.5]);
}

#[tokio::test]
async fn scripted_frame_stream_yields_frames_then_close() {
    use super::BrokerFrameStream;

    let mut stream = ScriptedFrameStream::new(vec!["one".into(), "two".into()]);
    assert_eq!(stream.next_text().await.unwrap().as_deref(), Some("one"));
    assert_eq!(stream.next_text().await.unwrap().as_deref(), Some("two"));
    assert!(stream.next_text().await.unwrap().is_none());
    assert!(stream.next_text().await.unwrap().is_none());
}

#[test]
fn system_clock_reports_nonnegative_unix_millis() {
    assert!(SystemClock.now_millis() > 0);
}
