//! Authority time, rather than an ingestion worker's wall clock, owns expiry.
use axum::{
    body::Bytes,
    extract::State,
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
    Router,
};
use azure_core::auth::{AccessToken, TokenCredential};
use lake_ingest_jobs::{BudgetState, JobStore};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc, Mutex,
};

#[derive(Debug)]
struct Credential;
#[async_trait::async_trait]
impl TokenCredential for Credential {
    async fn get_token(&self, _: &[&str]) -> azure_core::Result<AccessToken> {
        Ok(AccessToken::new(
            "fake",
            time::OffsetDateTime::UNIX_EPOCH + time::Duration::days(30000),
        ))
    }
    async fn clear_cache(&self) -> azure_core::Result<()> {
        Ok(())
    }
}
struct Authority {
    seconds: AtomicU64,
    state: Mutex<Option<BudgetState>>,
}
async fn blob(State(authority): State<Arc<Authority>>, method: Method, body: Bytes) -> Response {
    let mut response = if method == Method::GET {
        match authority.state.lock().unwrap().as_ref() {
            Some(state) => ([("etag", "v1")], serde_json::to_vec(state).unwrap()).into_response(),
            None => StatusCode::NOT_FOUND.into_response(),
        }
    } else {
        *authority.state.lock().unwrap() = Some(serde_json::from_slice(&body).unwrap());
        StatusCode::CREATED.into_response()
    };
    response.headers_mut().insert(
        "date",
        httpdate::fmt_http_date(
            std::time::UNIX_EPOCH
                + std::time::Duration::from_secs(authority.seconds.load(Ordering::SeqCst)),
        )
        .parse()
        .unwrap(),
    );
    response
}

#[tokio::test]
async fn blob_date_controls_rolling_expiry_even_when_workers_have_a_different_wall_clock() {
    let authority = Arc::new(Authority {
        seconds: AtomicU64::new(42),
        state: Mutex::new(None),
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = Router::new()
        .fallback(any(blob))
        .with_state(authority.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let store = JobStore::new(format!("http://{address}"), Arc::new(Credential)).unwrap();
    let scope = "c".repeat(64);
    assert_eq!(
        store
            .reserve_requests(&scope, 10000, 10000)
            .await
            .unwrap()
            .count,
        10000
    );
    {
        let state = authority.state.lock().unwrap();
        let state = state.as_ref().unwrap();
        assert_eq!(state.observed_at, 42_000_000);
        assert_eq!(state.charges[0].release_after, 108_000_000);
    }
    authority.seconds.store(107, Ordering::SeqCst);
    assert_eq!(
        store
            .reserve_requests(&scope, 10000, 1)
            .await
            .unwrap()
            .count,
        0
    );
    authority.seconds.store(108, Ordering::SeqCst);
    assert_eq!(
        store
            .reserve_requests(&scope, 10000, 10000)
            .await
            .unwrap()
            .count,
        10000
    );
    authority.seconds.store(43, Ordering::SeqCst);
    assert_eq!(
        store
            .reserve_requests(&scope, 10000, 1)
            .await
            .unwrap()
            .count,
        0
    );
    server.abort();
}
