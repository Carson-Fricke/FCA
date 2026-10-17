//! Production transport behavior against a loopback provider HTTP service.

use axum::{http::StatusCode, response::IntoResponse, routing::get, Json, Router};
use lake_providers::{AlpacaTransport, ReqwestTransport, TransportFailure};
use serde_json::{json, Value};

#[tokio::test(flavor = "current_thread")]
async fn concurrent_responses_preserve_decoding_and_http_error_policy() {
    let expected = json!({"trades": (0..10_000).collect::<Vec<_>>()});
    let body = expected.clone();
    let router = Router::new()
        .route("/valid", get(move || async move { Json(body) }))
        .route("/empty", get(|| async { StatusCode::NO_CONTENT }))
        .route("/malformed", get(|| async { "invalid JSON" }))
        .route(
            "/limited",
            get(|| async {
                (
                    StatusCode::TOO_MANY_REQUESTS,
                    [("retry-after", "2")],
                    "busy",
                )
                    .into_response()
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let transport = ReqwestTransport::new(2.0);
    let urls = ["valid", "empty", "malformed", "limited"].map(|path| format!("{base}/{path}"));
    let results = futures_util::future::join_all(urls.iter().map(|url| async {
        let allocation =
            std::sync::Arc::new(lake_pipeline::Pipeline::global().response().await.unwrap());
        transport.get(url, &[], &[], allocation).await
    }))
    .await;
    server.abort();
    assert_eq!(results[0].as_ref().unwrap().body, expected);
    assert_eq!(results[1].as_ref().unwrap().status, 204);
    assert_eq!(results[1].as_ref().unwrap().body, Value::Null);
    assert!(matches!(&results[2], Err(TransportFailure::Protocol(_))));
    let limited = results[3].as_ref().unwrap();
    assert_eq!(limited.status, 429);
    assert_eq!(limited.retry_after.as_deref(), Some("2"));
    assert_eq!(limited.body, Value::Null);
}
