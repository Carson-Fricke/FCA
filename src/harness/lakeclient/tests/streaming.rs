//! Streaming consumption through the HTTP port, with no production data traffic.
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::{body::Body, routing::get, Router};
use harness_lakeclient::testing::{bars_stream, FakeBarRow, FakeLakeConfig, FakeLakeServer};
use harness_lakeclient::{LakeClient, LakeClientConfig, ReadSpec};

#[tokio::test]
async fn consumes_a_batch_before_the_server_can_finish_the_response() {
    let payload = bars_stream("SPY", &[FakeBarRow::new(0, "10", "11", "9", "10", "1")]);
    let (send, receive) = tokio::sync::oneshot::channel();
    let receive = Arc::new(Mutex::new(Some(receive)));
    let app = Router::new().route(
        "/v1/read",
        get(move || {
            let payload = payload.clone();
            let receive = receive.lock().unwrap().take().unwrap();
            async move {
                let first = payload[..payload.len() - 8].to_vec();
                let stream = futures_util::stream::unfold(
                    (Some(first), Some(receive)),
                    |(first, receive)| async move {
                        if let Some(first) = first {
                            Some((Ok::<_, std::io::Error>(first), (None, receive)))
                        } else if let Some(receive) = receive {
                            receive.await.unwrap();
                            Some((Ok(vec![255, 255, 255, 255, 0, 0, 0, 0]), (None, None)))
                        } else {
                            None
                        }
                    },
                );
                Body::from_stream(stream)
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let observations = Arc::new(Mutex::new(Vec::new()));
    let captured = observations.clone();
    let client =
        LakeClient::new(LakeClientConfig::new(format!("http://{address}"), "test").unwrap())
            .unwrap()
            .with_progress(Arc::new(move |snapshot| {
                captured.lock().unwrap().push(snapshot);
                Ok(())
            }));
    let mut send = Some(send);
    let mut rows = 0;
    tokio::time::timeout(
        Duration::from_secs(2),
        client.visit_arrow(
            &ReadSpec::new("equity", "bars", "SPY", "2024-01-02", "2024-01-02"),
            |batch| {
                rows += batch.num_rows();
                send.take().unwrap().send(()).unwrap();
                Ok(())
            },
        ),
    )
    .await
    .expect("batch must be consumed before EOF")
    .unwrap();
    assert_eq!(rows, 1);
    let observations = observations.lock().unwrap();
    let last = observations.last().unwrap();
    assert_eq!(last.rows, 1);
    assert!(last.bytes > 0);
    assert!(last.completed);
    assert_eq!(last.request_timeout_seconds, Some(60));
    server.abort();
}

#[test]
fn truncated_stream_does_not_report_success_or_retry() {
    let mut payload = bars_stream("SPY", &[FakeBarRow::new(0, "10", "11", "9", "10", "1")]);
    payload.truncate(payload.len() - 12);
    let server = FakeLakeServer::start(FakeLakeConfig {
        read_bodies: vec![(200, payload)],
        ..FakeLakeConfig::new("test")
    });
    let client = harness_lakeclient::BlockingLakeClient::new(
        LakeClientConfig::new(server.base_url(), "test").unwrap(),
    )
    .unwrap();
    let observations = Arc::new(Mutex::new(Vec::new()));
    let captured = observations.clone();
    let client = client.with_progress(Arc::new(move |snapshot| {
        captured.lock().unwrap().push(snapshot);
        Ok(())
    }));
    let result = client.visit_arrow(
        &ReadSpec::new("equity", "bars", "SPY", "2024-01-02", "2024-01-02"),
        |_| Ok(()),
    );
    assert!(result.is_err());
    assert_eq!(server.requests().len(), 1);
    let observations = observations.lock().unwrap();
    let last = observations.last().unwrap();
    assert!(last.bytes > 0);
    assert!(!last.completed);
}
