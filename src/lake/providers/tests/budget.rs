//! A retry is another provider attempt; a missing fleet permit fails closed before transport.
use lake_contracts::DateTime;
use lake_providers::{
    AlpacaClient, AlpacaTransport, BoxFuture, ProviderConfig, ProviderError, RequestBudget,
    RequestGovernor, TransportFailure, TransportResponse,
};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

struct Budget {
    allowed: usize,
    consumed: AtomicUsize,
}
impl RequestBudget for Budget {
    fn acquire(&self) -> BoxFuture<'_, Result<(), ProviderError>> {
        Box::pin(async move {
            if self.consumed.fetch_add(1, Ordering::SeqCst) >= self.allowed {
                Err(ProviderError::Configuration("budget unavailable".into()))
            } else {
                Ok(())
            }
        })
    }
}
struct Provider {
    calls: AtomicUsize,
}
impl AlpacaTransport for Provider {
    fn get<'a>(
        &'a self,
        _: &'a str,
        _: &'a [(String, String)],
        _: &'a [(String, String)],
        _: Arc<lake_pipeline::Reservation>,
    ) -> BoxFuture<'a, Result<TransportResponse, TransportFailure>> {
        Box::pin(async move {
            let attempt = self.calls.fetch_add(1, Ordering::SeqCst);
            Ok(TransportResponse {
                allocation: None,
                status: if attempt == 0 { 503 } else { 200 },
                retry_after: None,
                body: serde_json::to_vec(
                    &serde_json::json!({"quotes":{"AAPL":[]},"next_page_token":null}),
                )
                .unwrap(),
            })
        })
    }
}
#[tokio::test]
async fn retries_require_fleet_permits_and_budget_failure_never_dispatches() {
    for allowed in [2, 1] {
        let budget = Arc::new(Budget {
            allowed,
            consumed: AtomicUsize::new(0),
        });
        let provider = Arc::new(Provider {
            calls: AtomicUsize::new(0),
        });
        let config = ProviderConfig {
            api_key: Some(format!("budget-behavior-{allowed}")),
            requests_per_minute: Some(10_000),
            max_attempts: 2,
            backoff_seconds: 0.0,
            max_backoff_seconds: 0.0,
            ..ProviderConfig::default()
        };
        let client = AlpacaClient::new(
            config,
            provider.clone(),
            RequestGovernor::with_budget(10_000, budget.clone()).unwrap(),
        )
        .unwrap();
        let time = DateTime::from_micros(0);
        let result = client.fetch_quotes(&["AAPL"], time, time).await;
        if allowed == 2 {
            assert!(result.unwrap().is_empty());
        } else {
            assert!(matches!(result, Err(ProviderError::Configuration(_))));
        }
        assert_eq!(provider.calls.load(Ordering::SeqCst), allowed);
        assert_eq!(budget.consumed.load(Ordering::SeqCst), 2);
    }
}
