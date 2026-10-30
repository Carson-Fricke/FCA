//! Deliver committed terminal events to Thesis. This adapter never queries a
//! running job, waits for execution, or submits work. Failed notification delivery
//! leaves the outbox event intact for the existing control reconciler to retry.

use super::backend::{error, Result};
use super::ExecutionStore;
use harness_execution::store::JobCompletion;

pub fn deliver(store: &ExecutionStore, event: &JobCompletion) -> Result<()> {
    let Ok(url) = std::env::var("HARNESS_COMPLETION_URL") else {
        return Ok(());
    };
    let token = std::env::var("HARNESS_COMPLETION_TOKEN").map_err(|_| {
        error("HARNESS_COMPLETION_TOKEN is required when completion delivery is configured")
    })?;
    let agent = ureq::AgentBuilder::new()
        .redirects(0)
        .timeout(std::time::Duration::from_secs(10))
        .build();
    let response = agent
        .post(&url)
        .set("Authorization", &format!("Bearer {token}"))
        .send_json(serde_json::to_value(event).map_err(error)?)
        .map_err(|_| {
            error(format!(
                "completion delivery failed for {}; event retained",
                event.job_id
            ))
        })?;
    if response.status() != 204 {
        return Err(error(
            "completion delivery was not durably acknowledged; event retained",
        ));
    }
    store.acknowledge_completion(event)
}

pub fn retry_pending(store: &ExecutionStore) -> Result<()> {
    if std::env::var_os("HARNESS_COMPLETION_URL").is_none() {
        return Ok(());
    }
    for event in store.pending_completions()?.into_iter().take(64) {
        // Stop on unavailable intake; one failed POST is enough to retain the
        // remaining events. This retries delivery, never execution or status.
        deliver(store, &event)?;
    }
    Ok(())
}
