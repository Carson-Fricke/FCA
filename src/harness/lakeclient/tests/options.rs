//! Option input acquisition waits on the retained external job and validates acknowledgements.
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::get;
use axum::routing::post;
use axum::Json;
use axum::Router;
use harness_lakeclient::LakeClient;
use harness_lakeclient::LakeClientConfig;
use serde_json::json;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::sync::Arc;

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

/// A native host supplies typed capabilities; option resolution must use them
/// for the initial request, subsequent resolution and retained-job inspection.
struct NativeOptions {
    calls: AtomicUsize,
    mismatched: bool,
}
#[async_trait::async_trait]
impl harness_lakeclient::MarketOptions for NativeOptions {
    async fn resolve_option(
        &self,
        _: &harness_contracts::OptionSelector,
    ) -> Result<harness_lakeclient::OptionResolution, harness_lakeclient::LakeClientError> {
        if self.calls.fetch_add(1, Ordering::SeqCst) == 0 {
            Ok(harness_lakeclient::OptionResolution::Pending {
                job_id: "option-one".into(),
            })
        } else {
            let resolved = serde_json::from_value(json!({"symbol":"SPY240216C00100000","underlying":"SPY","expiration":"2024-02-16","strike":"100","right":"call","multiplier":100,"as_of":"2024-01-18T21:00:00Z","delta":null,"quote_time":"2024-01-18T20:59:00Z","method":"test"})).unwrap();
            Ok(harness_lakeclient::OptionResolution::Resolved(resolved))
        }
    }
}
#[async_trait::async_trait]
impl harness_lakeclient::MarketAcquisition for NativeOptions {
    async fn ensure(
        &self,
        _: &harness_lakeclient::EnsureSpec,
    ) -> Result<harness_lakeclient::EnsureResponse, harness_lakeclient::LakeClientError> {
        panic!("unexpected acquisition")
    }
    async fn coverage(
        &self,
        _: &harness_lakeclient::EnsureSpec,
    ) -> Result<harness_lakeclient::CoverageReport, harness_lakeclient::LakeClientError> {
        panic!("unexpected coverage")
    }
    async fn job(
        &self,
        id: &str,
    ) -> Result<harness_lakeclient::JobStatus, harness_lakeclient::LakeClientError> {
        assert_eq!(id, "option-one");
        Ok(harness_lakeclient::JobStatus {
            job_id: if self.mismatched { "other" } else { id }.into(),
            state: "complete".into(),
            error_message: None,
        })
    }
}
#[async_trait::async_trait]
impl harness_lakeclient::MarketReference for NativeOptions {
    async fn universe(&self, _: &str) -> Result<Vec<String>, harness_lakeclient::LakeClientError> {
        panic!("unexpected reference read")
    }
    async fn facts(
        &self,
        _: &[String],
        _: &str,
    ) -> Result<harness_lakeclient::Facts, harness_lakeclient::LakeClientError> {
        panic!("unexpected facts read")
    }
}
#[async_trait::async_trait]
impl harness_lakeclient::MarketSnapshots for NativeOptions {
    async fn snapshot(
        &self,
        _: &harness_lakeclient::ReadSpec,
    ) -> Result<harness_lakeclient::MarketDataSnapshot, harness_lakeclient::LakeClientError> {
        panic!("unexpected snapshot")
    }
    fn source(&self) -> &dyn harness_market_reader::PartitionSource {
        panic!("unexpected object read")
    }
}
#[tokio::test]
async fn native_option_resolution_uses_typed_ports_and_retained_identity() {
    for mismatched in [false, true] {
        let access = Arc::new(NativeOptions {
            calls: AtomicUsize::new(0),
            mismatched,
        });
        let client = LakeClient::from_access(access.clone()).unwrap();
        let selector = serde_json::from_value(json!({"underlying":"SPY","right":"call","as_of":"2024-01-18T21:00:00Z","expiration":"2024-02-16","strike":"100"})).unwrap();
        let result = client.resolve_option(&selector).await;
        if mismatched {
            assert!(result.unwrap_err().to_string().contains("ID mismatch"));
            assert_eq!(access.calls.load(Ordering::SeqCst), 1);
        } else {
            assert_eq!(result.unwrap().symbol, "SPY240216C00100000");
            assert_eq!(access.calls.load(Ordering::SeqCst), 2);
        }
    }
}
