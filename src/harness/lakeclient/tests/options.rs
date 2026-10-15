//! Option input acquisition waits on the retained external job and validates acknowledgements.
use axum::{
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use harness_lakeclient::{LakeClient, LakeClientConfig};
use serde_json::json;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

#[tokio::test]
async fn option_acquisition_waits_and_rejects_another_jobs_acknowledgement() {
    for mismatched in [false, true] {
        let calls = Arc::new(AtomicUsize::new(0));
        let requests = calls.clone();
        let app=Router::new().route("/v1/options/resolve",post(move || {
            let calls=requests.clone();async move {
                if calls.fetch_add(1,Ordering::SeqCst)==0 {(StatusCode::ACCEPTED,Json(json!({"job_id":"option-one","state":"queued"}))).into_response()}
                else {Json(json!({"symbol":"SPY240216C00100000","underlying":"SPY","expiration":"2024-02-16","strike":"100","right":"call","multiplier":100,"as_of":"2024-01-18T21:00:00Z","delta":null,"quote_time":"2024-01-18T20:59:00Z","method":"test"})).into_response()}
            }
        })).route("/v1/jobs/option-one",get(move ||async move {Json(json!({"job_id":if mismatched {"other-job"}else{"option-one"},"state":"complete"}))}));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client =
            LakeClient::new(LakeClientConfig::new(format!("http://{address}"), "test").unwrap())
                .unwrap();
        let selector=serde_json::from_value(json!({"underlying":"SPY","right":"call","as_of":"2024-01-18T21:00:00Z","expiration":"2024-02-16","strike":"100"})).unwrap();
        let result = client.resolve_option(&selector).await;
        if mismatched {
            assert!(result.is_err());
            assert_eq!(calls.load(Ordering::SeqCst), 1);
        } else {
            assert_eq!(result.unwrap().symbol, "SPY240216C00100000");
            assert_eq!(calls.load(Ordering::SeqCst), 2);
        }
        server.abort();
    }
}
