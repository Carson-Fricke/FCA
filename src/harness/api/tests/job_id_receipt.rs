use std::path::PathBuf;
use std::sync::Arc;

use api::local::{NativeBacktestSubmitter, NativeScreeningSubmitter, NativeStudySubmitter};
use mcp::store::{InMemoryJobStore, JobStore};
use mcp::tools::{BacktestSubmitter, ScreeningSubmitter, StudySubmitter};
use serde_json::{json, Value};

fn write_temp(name: &str, document: &Value) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-job-id-receipt-{}-{name}.json",
        std::process::id()
    ));
    std::fs::write(&path, document.to_string()).unwrap();
    path
}

fn replay_document(shell: &str) -> Value {
    let subscriptions = if shell == "study" {
        json!([{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}])
    } else {
        json!([{"data_type": "quote", "symbols": ["SPY"]}])
    };
    json!({
        "schema_version": 1,
        "shell": shell,
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-04T21:00:00Z",
        "data": {"symbols": ["SPY"], "subscriptions": subscriptions}
    })
}

fn screen_document() -> Value {
    let source = "// screening source\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "job-id-receipt",
        "as_of": "2024-01-19",
        "bar_intervals": ["1d"],
        "lookback_days": 20,
        "rank_metric": "close",
        "source": source,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "0".repeat(64)),
        "definition_hash": "0".repeat(64)
    })
}

#[test]
fn backtest_receipt_echoes_the_submitted_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeBacktestSubmitter::new(store.clone(), Arc::new(Wake));
    let path = write_temp("backtest", &replay_document("backtest"));
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-receipt-backtest"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-receipt-backtest");
    assert_eq!(receipt["state"], "queued");
    assert_eq!(receipt["kind"], "backtest");
    assert_eq!(
        store.get("job-receipt-backtest").unwrap().job_id,
        "job-receipt-backtest"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn study_receipt_echoes_the_submitted_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeStudySubmitter::new(store.clone(), Arc::new(Wake));
    let path = write_temp("study", &replay_document("study"));
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-receipt-study"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-receipt-study");
    assert_eq!(receipt["kind"], "study");
    assert_eq!(
        store.get("job-receipt-study").unwrap().job_id,
        "job-receipt-study"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn screen_receipt_echoes_the_submitted_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeScreeningSubmitter::new(store.clone(), Arc::new(Wake));
    let path = write_temp("screen", &screen_document());
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-receipt-screen"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-receipt-screen");
    assert_eq!(receipt["kind"], "screen");
    assert_eq!(
        store.get("job-receipt-screen").unwrap().job_id,
        "job-receipt-screen"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn submitted_job_id_overrides_a_document_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeBacktestSubmitter::new(store.clone(), Arc::new(Wake));
    let mut document = replay_document("backtest");
    document["job_id"] = json!("job-document");
    let path = write_temp("backtest-overlay", &document);
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-submitted"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-submitted");
    assert!(store.get("job-document").is_none());
    assert_eq!(store.get("job-submitted").unwrap().job_id, "job-submitted");
    std::fs::remove_file(path).unwrap();
}

struct Wake;
impl api::execution::Wake for Wake {
    fn wake(&self) -> Result<(), String> {
        Ok(())
    }
}
